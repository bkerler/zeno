# Credits: Josh Watson @joshwatson
from binaryninja import SSAVariable, Variable, MediumLevelILOperation
from src.avd.helper import binjaWrapper


# TODO Make forward slice to visit through non blacklisted functions
def do_forward_slice(instruction, function):
    # if no variables written, return the empty set.
    if not instruction.ssa_form.vars_written:
        return set()

    instruction_queue = {
        use for var in instruction.ssa_form.vars_written if var.var.name
        for use in function.ssa_form.get_ssa_var_uses(var)
    }

    visited_instructions = {instruction.ssa_form.instr_index}

    while instruction_queue:
        visit_index = instruction_queue.pop()

        if visit_index is None or visit_index in visited_instructions:
            continue

        instruction_to_visit = function[visit_index]

        if instruction_to_visit is None:
            continue

        instruction_queue.update(
            (
                use for var in instruction_to_visit.ssa_form.vars_written
                if var.var.name
                for use in function.ssa_form.get_ssa_var_uses(var)
            )
        )

        visited_instructions.add(visit_index)

    return visited_instructions


def handle_backward_functions(bv, var_index, function):
    print("Handling Backward Function")
    for refs in bv.get_code_refs(function.source_function.start):
        instruction = binjaWrapper.get_medium_il_instruction(bv, refs.address)
        # instruction = refs.function.get_low_level_il_at(refs.address).mapped_medium_level_il
        call_instr_index = instruction.instr_index
        new_var = instruction.ssa_form.vars_read[var_index]
        new_instr_index = instruction.function.ssa_form.get_ssa_var_definition(new_var)
        new_instr = instruction.function.ssa_form[new_instr_index]
        return do_backward_slice(bv, new_instr, new_var, new_instr.function)
    return set()


def get_sources_of_variable(bv, var):
    sources = []
    for bb in var.function.medium_level_il.ssa_form:
        for instr in bb:
            for v in (instr.vars_read + instr.vars_written):
                if isinstance(v, Variable):
                    if v.identifier == var.identifier:
                        visited = do_forward_slice(instr, v.function.medium_level_il.ssa_form)
                        for index in visited:
                            call = v.function.medium_level_il.ssa_form[index]
                            if call.operation == MediumLevelILOperation.MLIL_CALL_SSA:
                                sources.append(bv.get_symbol_at(call.dest.constant).name)
                                # Resolv call.dest
    return sources

def get_ssa_manual_var_uses(func, var):
    variables = []
    for bb in func:
        for instr in bb:
            for v in (instr.vars_read + instr.vars_written):
                if v.identifier == var.identifier:
                    variables.append(instr.instr_index)
    return variables

def get_manual_var_uses(func, var):
    variables = []
    for bb in func:
        for instr in bb:
            for v in (instr.vars_read + instr.vars_written):
                if v.identifier == var.identifier:
                    variables.append(instr.instr_index)
    return variables

def do_backward_slice(bv, instruction, var_pass, func):
    """

    :param bv:
    :param instruction:
    :param var_pass:
    :param func:
    :return:
    """
    # TODO var_pass kinda unnused wtf?
    if var_pass is None:
        print(instruction.ssa_form.operands[1])
        if not isinstance(instruction.ssa_form.operands[1], SSAVariable):
            print("Failed ! No SSA Var")
            return set()
        var_pass = instruction.ssa_form.operands[1]
        instruction_queue = set([instruction.ssa_form.instr_index])
    else:
        # Search for exactly one Operant
        instruction_queue = set([func.get_ssa_var_definition(var_pass)])

    variables = set()
    ## TODO Current Version contains a vars_read Bug on Stack Offsets. Hence the following is commented out.. on Bugfix use this again since it does make more sense overall!

    visited_instructions = []
    searched_var = None
    args = []
    for i in range(0, len(func.non_ssa_form.source_function.parameter_vars)):
        args.append(SSAVariable(func.non_ssa_form.source_function.parameter_vars[i], 0))

    while instruction_queue:
        visit_index = instruction_queue.pop()
        if visit_index is None or visit_index in visited_instructions:
            continue
        instruction_to_visit = func[visit_index]
        if instruction_to_visit is None:
            continue

        for new_var in instruction_to_visit.ssa_form.vars_read:
            try:
                # TODO Might fail on other slices. Debug it ... this will return only one passed Variable
                if isinstance(new_var, Variable):
                    searched_var = new_var
                    visited_instructions.append(instruction_to_visit.instr_index)
                    continue
                #else:
                    # Extracting the real var out of the SSA form and check it against the function parameters.
                    # If this is true we return the function parameter.
                    # TODO do function backtracing to get the input
                    #if new_var.var in func.source_function.parameter_vars:
                    #    searched_var = new_var

                instruction_queue.add(
                    func.get_ssa_var_definition(
                        new_var
                    )
                )
                variables.update(
                    [(var.var.identifier, var.version)
                     for var in instruction_to_visit.ssa_form.vars_read]
                )
                if instruction_to_visit not in visited_instructions:
                    visited_instructions.append(instruction_to_visit.instr_index)
                # print(visited_instructions)
                if new_var in args:
                    # print("Found Function End")
                    for a in handle_backward_functions(bv, args.index(new_var), func):
                        if isinstance(a, Variable):
                            searched_var = a
                            continue
                        if a not in visited_instructions:
                            visited_instructions.append(a)
            except AttributeError as e:
                # Might be final variable... lets check
                print("ERROR")
                pass
            except Exception as e:
                print(e)

                # print(function_Variables)
    # for ea in visited_instructions:
    #     print(function[ea])
    return searched_var, visited_instructions