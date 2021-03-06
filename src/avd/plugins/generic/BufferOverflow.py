from src.avd.plugins import Plugin
from src.avd.reporter.vulnerability import Vulnerability
from src.avd.helper import binjaWrapper, sources
import re
import collections
import traceback
from src.avd.core.sliceEngine.loopDetection import loop_analysis
from binaryninja import MediumLevelILOperation, RegisterValueType, SSAVariable
from sys import maxsize
from tqdm import tqdm

__all__ = ['PluginBufferOverflow']


class BoParams(object):
    def __init__(self, dst=None, src=None, n=None, format_ident=None):
        if dst is not None:
            self.dst = dst
        if src is not None:
            self.src = src
        if n is not None:
            self.n = n
        if format is not None:
            self.Format = format_ident


class FakeRegister(object):
    def __init__(self, name, constant=False):
        self.name = name
        self.is_constant = constant


def get_params(bv, addr):
    return binjaWrapper.get_medium_il_instruction(bv, addr.address).params



def parse_format_string(s, params):
    string_match = re.findall(r'%[0-9]*[diuoxfegacspn]', s, re.I)
    format_vars = collections.OrderedDict()
    for i, form in enumerate(string_match):
        format_vars[form] = params[i]
    return format_vars


def calc_size(var, func):
    if not var:
        return None
    if SSAVariable == type(var):
        var = var.var
    try:
        if len(func.stack_layout) - 1 == func.stack_layout.index(var):
            return abs(var.storage)
        else:
            return abs(var.storage) - abs(func.stack_layout[func.stack_layout.index(var) + 1].storage)
    except ValueError:
        # For some odd reason BN does screw up the stack layout.. bug?
        return 0


# TODO Move to PrettyPrinter Class
def print_f_call(arg):
    arg_iter = iter(arg[:])
    fun_c = next(arg_iter)
    fun_c += "("
    for i, ar in enumerate(arg_iter, 1):
        try:
            fun_c += ar
        except TypeError:
            if ar:
                fun_c += ar.name
            else:
                fun_c += "None"
        if i < (len(arg)-1):
            fun_c += ', '
    fun_c += ");"
    return fun_c


class PluginBufferOverflow(Plugin):
    name = "PluginBufferOverflow"
    display_name = "Buffer Overflow"
    cmd_name = "bo"
    cmd_help = "Search for Known Buffer Overflow patterns"

    def __init__(self, bv=None, args=None):
        super(PluginBufferOverflow, self).__init__(bv)
        self.arch_offsets = {
            'armv7': 4,
            'aarch64': 4,
            'x86_64': 0,
            'x86': 0,
            'thumb2': 0,
        }
        self.bv = bv
        self.args = args
        self.bo_symbols = {
            "_memmove": BoParams(dst=0, src=1, n=2),
            "memmove": BoParams(dst=0, src=1, n=2),
            "_memcpy": BoParams(dst=0, src=1, n=2),
            "memcpy": BoParams(dst=0, src=1, n=2),
            "_strncpy": BoParams(dst=0, src=1, n=2),
            "strncpy": BoParams(dst=0, src=1, n=2),
            "_strcpy": BoParams(dst=0, src=1),
            "strcpy": BoParams(dst=0, src=1),
            "_strcat": BoParams(dst=0, src=1),
            "strcat": BoParams(dst=0, src=1),  # TODO: strcat Needs to special checked if buffer was filled before!
            "_strncat": BoParams(dst=0, src=1, n=2),
            "strncat": BoParams(dst=0, src=1, n=2),
            "_sprintf": BoParams(dst=0, format_ident=1, src=2),
            "sprintf": BoParams(dst=0, format_ident=1, src=2),  # TODO: Multiple Args & Calc Length of FormatString
            "_snprintf": BoParams(dst=0, src=3, n=1),
            "snprintf": BoParams(dst=0, src=3, n=1),
            "_vsprintf": BoParams(dst=0, src=2),
            "vsprintf": BoParams(dst=0, src=2),
            "_fgets": BoParams(dst=0, n=1),
            "fgets": BoParams(dst=0, n=1),
            "gets": BoParams(dst=0),
            "_gets": BoParams(dst=0),
            "__isoc99_scanf": BoParams(format_ident=0),
        }

    def set_bv(self, bv):
        self.bv = bv

    # TODO Add default Blacklist to avoid Parsing e.g. libc
    def deep_function_analysis(self):
        for func in tqdm(self.bv.functions, desc=self.name + " Deep Analysis", leave=False):
            func_mlil = func.medium_level_il
            for bb in tqdm(func_mlil, desc=self.name + " Deep: Basic Blocks in function", leave=False):
                for instr in bb:
                    # MLIL Store might be interesting due to Compiler optimizations
                    if instr.operation == MediumLevelILOperation.MLIL_STORE:
                        # Check that Source is not Static # TODO might miss src > dest
                        if instr.src.possible_values.type == RegisterValueType.UndeterminedValue:
                            # Instruction should be in a loop. Otherwise a BoF is unlikely
                            if loop_analysis(bb):
                                # Slice to Source
                                # TODO Currently only works for MLIL_STORE (e.g. <il: [rdi_1].q = [rsi].q>)
                                src_visited_instr = self.slice_engine.do_backward_slice_with_variable(
                                    instr,
                                    func_mlil.ssa_form,
                                    instr.ssa_form.vars_read[1],
                                    list()
                                )
                                dst_visited_instr = self.slice_engine.do_backward_slice_with_variable(
                                    instr,
                                    func_mlil.ssa_form,
                                    instr.ssa_form.vars_read[0],
                                    list()
                                )
                                # TODO ugly hack.
                                # Just take the last Sliced Variable (might fail when tracing functions backwards)
                                # TODO This is just a hotfix when src or dst is None.
                                # TODO problem by CWE121_Stack_Based_Buffer_Overflow__char_type_overrun_memcpy_01
                                if not src_visited_instr or not dst_visited_instr:
                                    continue
                                else:
                                    if len(dst_visited_instr[-1].instr.vars_read) == 0 or \
                                            len(src_visited_instr[-1].instr.vars_read) == 0:
                                        # Architecture spezific where Instructions
                                        # can have direkt assignments to Registers
                                        # TODO Fix for partial assigments in ARM e.g [r4 + 0x11].b = (r6_1).b
                                        continue
                                    src = src_visited_instr[-1].instr.vars_read[0]
                                    dst = dst_visited_instr[-1].instr.vars_read[0]
                                if SSAVariable == type(src):
                                    src = src.var
                                if SSAVariable == type(dst):
                                    dst = dst.var
                                src_size = calc_size(src, src.function)
                                dst_size = calc_size(dst, dst.function)
                                if src_size > dst_size:
                                    # Might be an overflow. Lets Check if Source comes from a nasty function.
                                    # pretty print array
                                    cf = list(["memcpy"])
                                    cf.append(src)
                                    cf.append(dst)
                                    cf.append("<undetermined>")
                                    text = "{} 0x{:x}\t{}\n".format(func.name,
                                                                    instr.address, print_f_call(cf))
                                    text += "\t\tPotential Overflow!\n"
                                    text += "\t\t\tdst {} = {}\n".format(dst.name, dst_size)
                                    text += "\t\t\tsrc {} = {}\n".format(src.name, src_size)
                                    v = Vulnerability("Potential Overflow",
                                                      text,
                                                      instr,
                                                      "Deep Search found that the Source  Size: {} appears to be "
                                                      "bigger than the destination Size:"
                                                      " {}".format(calc_size(src, func), calc_size(dst, func)),
                                                      50)
                                    if not func_mlil.get_var_uses(src):
                                        # Probably dealing with a reference. Currently not implemented in BN.
                                        # Hence.. parsing manually
                                        # TODO port it to a function
                                        #func_mlil = func_mlil.ssa_form
                                        func_mlil_ssa = func_mlil.ssa_form
                                        for n in self.slice_engine.get_manual_var_uses(func_mlil, src):
                                            if n not in src_visited_instr:
                                                if src in func_mlil[n].vars_read:
                                                    for vs in func_mlil[n].vars_written:
                                                        for ea in self.slice_engine.do_forward_slice(func_mlil[n], func_mlil_ssa):
                                                            if func_mlil_ssa[ea].operation == MediumLevelILOperation.MLIL_CALL_SSA:
                                                                if self.bv.get_function_at(func_mlil_ssa[ea].dest.constant).name in sources.user_sources:
                                                                    # Check wheter it is in known user input sources Increase Probability
                                                                    v.append_reason(
                                                                        "The Source Location was used by a known Source")
                                                                    v.probability = 80
                                                else:
                                                    # TODO
                                                    # Written
                                                    pass
                                    self.append_vuln(v)

    @staticmethod
    def handle_single_destination(format_vars, ref, src_size, current_function):
        for f_str in format_vars:
            # TODO check if possible to delete
            size = 0
            # TODO Handle arch dependent max Size of int/double etc
            if "s" in f_str or "c" in f_str:
                try:
                    # Adjustment for the format string
                    size -= len(f_str)
                    # TODO Calculate correct size with delimited size
                    # if This fails there is prob no limitation
                    size += int(f_str[1:-1])
                except ValueError:
                    """
                    Fall Through since there might be no Format Limitation. Simple calc source size
                    """
                    size += src_size

            return size

    def handle_multi_destinations(self, format_vars, ref, current_function, cf):
        for f_str in format_vars:
            v = ref.function.get_stack_var_at_frame_offset(format_vars[f_str].offset,
                                                           current_function.start)
            if "s" in f_str or "c" in f_str:
                buf = ""
                try:
                    # if This fails there is prob no limitation
                    size = int(f_str[1:-1])
                except ValueError:
                    """
                    Fall Through since there might be no Format Limitation. Might be unlimited User input
                    """
                    size = maxsize
                dst_f_size = calc_size(v, current_function)
                if size >= dst_f_size:
                    cf.append(v)
                    overflow_size = "<unlimited>" if size - dst_f_size > maxsize / 2 else size - dst_f_size
                    text = "{} 0x{:x}\t{}\n".format(ref.function.name, ref.address, print_f_call(cf))
                    text += "\t\tPotential Overflow!\n"
                    text += "\t\t\tdst {} = {}\n".format(v.name, dst_f_size)
                    text += "\t\t\tn = {}\n".format(overflow_size)
                    instr = binjaWrapper.get_medium_il_instruction(self.bv, ref.address)
                    v = Vulnerability("Potential Overflow",
                                      text,
                                      instr,
                                      "Format String might overflow Variable "
                                      "written to {} with size {} by {} Bytes".format(v.name,
                                                                                      dst_f_size,
                                                                                      overflow_size),
                                      100)
                    self.vulns.append(v)

    def run(self, bv=None, args=None):
        if bv is None:
            raise Exception("No state was provided by Binary Ninja. Something must be wrong")
        super(PluginBufferOverflow, self).__init__(bv, args)
        if args:
            if args.deep:
                self.deep_function_analysis()

        arch_offset = self.arch_offsets[self.bv.arch.name]
        for syms in tqdm(self.bo_symbols, desc=self.name, leave=False):
            symbol = self.bv.get_symbol_by_raw_name(syms)
            if symbol is not None:
                for ref in tqdm(self.bv.get_code_refs(symbol.address),
                                desc=self.name + ": " + syms + " References", leave=False):
                    current_function = ref.function
                    addr = ref.address
                    try:
                        bo_src = self.bo_symbols.get(syms).src
                    except AttributeError:
                        bo_src = None
                    # TODO granularer
                    except:
                        traceback.print_exc()

                    try:
                        bo_n = self.bo_symbols.get(syms).n
                    except AttributeError:
                        bo_n = None
                        n = None
                    # TODO granularer
                    except:
                        traceback.print_exc()

                    try:
                        bo_format = self.bo_symbols.get(syms).Format
                    except AttributeError:
                        bo_format = None
                    # TODO granularer
                    except:
                        traceback.print_exc()

                    try:
                        bo_dst = self.bo_symbols.get(syms).dst
                    except AttributeError:
                        bo_dst = None
                    # TODO granularer
                    except:
                        traceback.print_exc()

                    cf = list([])
                    cf.append(syms)

                    if bo_dst is not None:
                        dst = current_function.get_parameter_at(addr, None, self.bo_symbols.get(syms).dst)
                        if 'StackFrameOffset' not in str(dst.type):
                            if hasattr(dst, "value"):
                                dst_var = FakeRegister("<const>")
                                dst_size = dst.value
                            elif 'UndeterminedValue' in str(dst.type):
                                dst_var = FakeRegister("<undetermined>")
                                dst_size = 0
                            else:
                                dst_var = FakeRegister(dst.reg)
                                dst_size = maxsize
                        else:
                            dst_var = ref.function.get_stack_var_at_frame_offset(dst.offset + arch_offset,
                                                                                 current_function.start)
                            if dst_var is None:
                                dst_var = ref.function.get_stack_var_at_frame_offset(dst.offset, current_function.start)
                            dst_size = calc_size(dst_var, current_function)
                        cf.append(dst_var)

                    if bo_src is None and bo_n is None and bo_format is None:
                        """
                        Handling unsafe uses of gets and fgets while only providing a single variable as destination
                        Buffer
                        """
                        text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                        text += "\t\tPotential Overflow!\n"
                        text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                        instr = binjaWrapper.get_medium_il_instruction(self.bv, ref.address)
                        v = Vulnerability("Potential Overflow",
                                          text,
                                          instr,
                                          "Uses of gets and fgets only passing the"
                                          " destination variable is highly critical",
                                          100)
                        self.vulns.append(v)
                        continue

                    if bo_src is not None:
                        src = current_function.get_parameter_at(addr, None, self.bo_symbols.get(syms).src)
                        if 'StackFrameOffset' not in str(src.type):
                            if hasattr(src, "value"):
                                src_var = FakeRegister("<const>")
                                src_size = src.value
                            elif 'UndeterminedValue' in str(src.type):
                                src_var = FakeRegister("<undetermined>")
                                src_size = 0
                            else:
                                src_var = FakeRegister(src.reg)
                                src_size = 0
                        else:
                            src_var = ref.function.get_stack_var_at_frame_offset(src.offset + arch_offset,
                                                                                 current_function.start)
                            if src_var is None:
                                src_var = ref.function.get_stack_var_at_frame_offset(src.offset, current_function.start)
                            src_size = calc_size(src_var, current_function)
                        cf.append(src_var)

                    if bo_n is not None:
                        n = current_function.get_parameter_at(addr, None, self.bo_symbols.get(syms).n)
                        if 'StackFrameOffset' not in str(n.type) and 'ConstantValue' not in str(n.type):
                            try:
                                if hasattr(n, "reg"):
                                    n = FakeRegister(n.reg, constant=n.reg.is_constant)
                                else:
                                    tmp_instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                                    n = tmp_instr.ssa_form.vars_read[self.bo_symbols.get(syms).n]
                                    n_val = "<undetermined>"
                                    # TODO delete
                                    # n = FakeRegister("<undetermined>", constant=n.is_constant)
                            # TODO Fix Exception to be more precise
                            except Exception as e:
                                # TODO Fix tracebacks
                                #traceback.print_exc()
                                try:
                                    real_param_name = get_params(self.bv, ref)[bo_n].src.name
                                    n = FakeRegister(real_param_name)
                                    n_val = real_param_name
                                except IndexError:
                                    # TODO binary ninja had a problem with correctly resolving the function parameters.
                                    # Need to try it manually
                                    continue
                                except AttributeError:
                                    # Can happen on if instructions beeing referenced
                                    continue

                        else:
                            if n.is_constant:
                                n_val = str(n.value)
                            else:
                                n_val = str(n)
                        cf.append(n_val)

                    # Print the function
                    # print("{} 0x{:x}\t{}".format(ref.function.name, addr, print_f_call(cf)))
                    # Handling Format Strings like scanf
                    if bo_format is not None:
                        params = []
                        for i in range(0, len(get_params(self.bv, ref))):
                            params.append(current_function.get_parameter_at(addr, None, i))
                        format_string = binjaWrapper.get_constant_string(self.bv,
                                                                         params[self.bo_symbols.get(syms).Format].value)
                        cf.insert(self.bo_symbols.get(syms).Format+1, "'" + format_string + "'")
                        params.pop(bo_format)
                        format_vars = parse_format_string(format_string, params)
                        if bo_dst is not None:
                            size = self.handle_single_destination(format_vars, ref, src_size, current_function)
                            if not size:
                                size = 0
                            size += len(format_string)
                            if size > dst_size:
                                text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                                text += "\t\tPotential Overflow!\n"
                                text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                                text += "\t\t\tsrc {} = {}\n".format(src_var.name, src_size)
                                text += "\t\t\ttotal_length = {}\n".format(size)
                                instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                                v = Vulnerability("Potential Overflow",
                                                  text,
                                                  instr,
                                                  "Format function {} can overflow the "
                                                  "destination Buffer with {} Bytes".format(syms, size - dst_size),
                                                  80)
                                self.vulns.append(v)
                            elif size == dst_size:
                                # Check if the Format String ends the string properly
                                last_format = list(format_vars.keys())[-1]
                                ending_strings = ["\n", "\r", "\x00"]
                                if not any(x in format_string[format_string.rfind(last_format) + len(last_format):] for x in
                                           ending_strings):
                                    text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                                    text += "\t\tPotential Overflow!\n"
                                    text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                                    instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                                    v = Vulnerability("Potential Overflow",
                                                      text,
                                                      instr,
                                                      "The source and destination size are equal. "
                                                      "There might be no Nullbyte/String delimiter",
                                                      60)
                                    self.vulns.append(v)
                        else:
                            self.handle_multi_destinations(format_vars, ref, current_function, cf)
                        continue

                    if bo_src is not None and bo_n is None and bo_dst is not None:
                        if src_size > dst_size:
                            """
                            Source Size is Bigger than dst_size
                            """
                            text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                            text += "\t\tPotential Overflow!\n"
                            text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                            text += "\t\t\tsrc {} = {}\n".format(src_var.name, src_size)
                            instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                            v = Vulnerability("Potential Overflow",
                                              text,
                                              instr,
                                              "The Source Buffer Size is bigger than the destination Buffer", 80)
                            self.vulns.append(v)
                            continue
                    elif bo_src is not None and bo_n is not None:
                        if SSAVariable == type(n):
                            """
                            N Value is undermined and source is not known. This will trigger a reverse Slice
                            to find the initiating part and check whether it might be attacker controlled against
                            the sources array 
                            """
                            # Follow N
                            instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                            slice_sources = self.slice_engine.get_sources2(bv, instr, n)
                            intersection_slices = [x for x in slice_sources if x in sources.user_sources]
                            if intersection_slices:
                                text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                                text += "\t\tPotential Overflow!\n"
                                text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                                text += "\t\t\tsrc {} = {}\n".format(src_var.name, src_size)
                                instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                                v = Vulnerability("Potential Overflow",
                                                  text,
                                                  instr,
                                                  "The amount of bytes copied might be user controlled through the "
                                                  "following sources {}\nFull Trace of functions for N is {}".format(
                                                      intersection_slices, slice_sources), 60)

                                if src_size > dst_size:
                                        v.probability = 90
                                        v.append_reason("The source buffer is also bigger than the destination Buffer")
                                self.vulns.append(v)
                                continue
                    if hasattr(n, "is_constant"):
                        if bo_n is not None and n.is_constant:
                            # N is constant
                            if n.value > dst_size:
                                """
                                N Value is bigger than dst size. 
                                """
                                text = "{} 0x{:x}\t{}\n".format(ref.function.name, addr, print_f_call(cf))
                                text += "\t\tPotential Overflow!\n"
                                text += "\t\t\tdst {} = {}\n".format(dst_var.name, dst_size)
                                text += "\t\t\tn {} = {}\n".format(str(n), n_val)
                                instr = binjaWrapper.get_medium_il_instruction(bv, ref.address)
                                v = Vulnerability("Potential Overflow",
                                                  text,
                                                  instr,
                                                  "The amount of Copied bytes is bigger than the destination Buffer",
                                                  100)
                                self.vulns.append(v)
                            elif n.value == dst_size:
                                """
                                Might indicate a Off-By-One 
                                """
                                # TODO
                                pass
                    if bo_src is not None:
                        if hasattr(src_var, "name") and hasattr(n, "name"):
                            if src_var.name == "<undetermined>" and n.name == "<undetermined>":
                                pass

