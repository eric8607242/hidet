# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Union, List, Dict, Sequence, Tuple, Set

from hidet.ir.type import tensor_pointer_type, void_pointer
from hidet.ir.expr import TensorElement, Expr, Var, Constant, scalar_var, convert, cast, deref
from hidet.ir.stmt import Stmt, AssignStmt, ForStmt, DeclareStmt, BufferStoreStmt, AssertStmt
from hidet.ir.task import Task
from hidet.ir.func import IRModule, Function
from hidet.ir.builders import FunctionBuilder, StmtBuilder
from hidet.ir.functors import ExprRewriter, ExprVisitor, ComputeVisitor, ComputeRewriter, TypeRewriter
from hidet.ir.tools import collect, rewrite, infer_type, simplify
from hidet.ir.compute import ScalarInput, TensorInput, GridCompute, ReduceCompute, ArgReduceCompute
from hidet.ir.compute import TensorNode, ScalarNode
from hidet.ir.primitives.runtime import request_cuda_workspace, request_cpu_workspace
from hidet.ir.dtypes import uint8, int32, int64
from hidet.utils import prod, DirectedGraph
from hidet.utils.namer import Namer


class ScalarComputeFound(Exception):
    pass


class GridComputeInlineChecker(ExprVisitor, ComputeVisitor):
    def check(self, gc: GridCompute) -> bool:
        """Check whether the grid compute can be inlined.

        A grid compute can be inlined if and only if it only directly accesses TensorNode but not ScalarNode.

        Parameters
        ----------
        gc: GridCompute
            The grid compute to be checked.

        Returns
        -------
        ret: bool
            True if the grid can be inlined, else False.
        """
        try:
            self.visit(gc.value)
        except ScalarComputeFound:
            return False
        return True

    def visit_TensorInput(self, node: TensorInput):
        return

    def visit_GridCompute(self, e: GridCompute):
        return

    def visit_ScalarInput(self, e: ScalarInput):
        return

    def visit_ReduceCompute(self, e: ReduceCompute):
        raise ScalarComputeFound()

    def visit_ArgReduceCompute(self, e: ArgReduceCompute):
        raise ScalarComputeFound()


def can_inline_grid_compute(gc: GridCompute) -> bool:
    return GridComputeInlineChecker().check(gc)


class GridComputeInliner(ExprRewriter, ComputeRewriter):
    def __init__(self):
        super().__init__()

    def inline(self, node: TensorNode):
        return self.visit(node)

    def visit_TensorElement(self, e: TensorElement):
        base = self(e.base)
        indices = [self(index) for index in e.indices]
        if isinstance(base, GridCompute):
            if can_inline_grid_compute(base):
                return rewrite(base.value, {axis: index for axis, index in zip(base.axes, indices)})
        return ExprRewriter.visit_TensorElement(self, e)


def inline_grid_compute(nodes: List[TensorNode]) -> List[TensorNode]:
    """Inline the grid compute that does not contain reduce.

    If a grid compute only does not contain scalar compute (e.g., reduce and arg_reduce), the grid will be
    inlined.

    For example:

    .. code-block:: python

         from hidet.ir.compute import tensor_input, compute
         a = tensor_input('a', 'float32', [10])
         b = compute('b', [10], lambda i: a[i] + 1)
         c = compute('c', [10], lambda i: b[i] * 2)
         d = inline_grid_compute(c)  # d is equivalent to
         e = compute('e', [10], lambda i: (a[i] + 1) * 2)

    Parameters
    ----------
    nodes: List[TensorNode]
        The compute nodes.

    Returns
    -------
    ret: List[TensorNode]
        The nodes after inlining.
    """
    inliner = GridComputeInliner()
    return [inliner.inline(node) for node in nodes]


class AutoScheduler:
    def __init__(self):
        super().__init__()
        self.ir_module: IRModule = IRModule()

    @staticmethod
    def get_accessed_nodes(node: TensorNode) -> List[TensorNode]:
        if isinstance(node, TensorInput):
            return []
        elif isinstance(node, GridCompute):
            e = node.value
            accessed_nodes: List[TensorNode] = collect(e, TensorNode, stop_when_found=True)
            accessed_nodes = list(set(accessed_nodes))  # remove duplicated ones
            return accessed_nodes
        else:
            raise NotImplementedError()

    @staticmethod
    def build_dag(outputs: Sequence[TensorNode]) -> DirectedGraph:
        dag = DirectedGraph()
        remain_nodes: List[TensorNode] = list(outputs)
        while len(remain_nodes) > 0:
            node = remain_nodes.pop()
            dag.add_node(node)
            accessed_nodes: List[TensorNode] = AutoScheduler.get_accessed_nodes(node)
            for accessed_node in accessed_nodes:
                if accessed_node not in dag:
                    remain_nodes.append(accessed_node)
                dag.add_edge(accessed_node, node)
        return dag

    @staticmethod
    def plan_memory(
        dag: DirectedGraph,  # pylint: disable=unused-argument
        order: Sequence[TensorNode],
        require_allocate: Set[TensorNode],
    ) -> Tuple[Expr, Dict[TensorNode, Expr]]:
        # dag has not been used in this simple plan.
        alignment_bytes: int = 128  # make sure each buffer aligns with 128 bytes
        allocated_bytes: Expr = int64(0)
        buffer_offset: Dict[TensorNode, Expr] = {}
        for tensor in order:
            if tensor not in require_allocate:
                continue
            buffer_offset[tensor] = allocated_bytes
            allocated_bytes = allocated_bytes + simplify(tensor.type.storage_bytes())
            allocated_bytes = (allocated_bytes + (alignment_bytes - 1)) // alignment_bytes * alignment_bytes
        return allocated_bytes, buffer_offset

    @staticmethod
    def allocate_tensors(
        fb: FunctionBuilder,
        device: str,
        buffer_bytes: Expr,
        buffer_offset: Dict[TensorNode, Expr],
        node_map: Dict[TensorNode, Var],
    ):
        if not (isinstance(buffer_bytes, Constant) and int(buffer_bytes) == 0):
            buffer = Var('buffer', tensor_pointer_type(dtype='uint8', shape=[buffer_bytes]))
            if device == 'cuda':
                space_ptr: Expr = request_cuda_workspace(nbytes=buffer_bytes, require_clean=False)
            elif device == 'cpu':
                space_ptr: Expr = request_cpu_workspace(nbytes=buffer_bytes, require_clean=False)
            else:
                raise ValueError()
            fb += DeclareStmt(buffer, init=cast(space_ptr, ~uint8))
        else:
            buffer = None
        for node in buffer_offset:
            if node in node_map:
                # this node is either an input or output tensor
                continue
            assert buffer is not None
            v = Var(node.name, ~node.type)
            node_map[node] = v
            fb += DeclareStmt(v, init=cast(~buffer[buffer_offset[node]], ~v.type.tensor_type.dtype))

    def schedule_task(self, task: Task, device: str) -> IRModule:
        # pylint: disable=too-many-locals, unnecessary-comprehension
        from hidet.ffi.packedfunc import ArgTypeCode

        self.ir_module.task = task

        # Inline the grid compute that does not contain reduce
        outputs: List[TensorNode] = inline_grid_compute(task.outputs)
        output_remap: Dict[TensorNode, TensorNode] = {a: b for a, b in zip(task.outputs, outputs)}
        updated_params = [output_remap[p] if p in output_remap else p for p in task.params]

        # Taking the TensorNode as node to construct the computation directed-acyclic-graph (DAG)
        # In the DAG, each node is a TensorNode and each edge (src, dst) indicates src is accessed by dst.
        dag = self.build_dag(outputs)

        # Get a topological order of the tensor nodes in the DAG
        order: List[TensorNode] = dag.topological_order()

        # Plan the memory for intermediate tensors
        # only allocate the memory for intermediate tensors
        require_allocate = set(node for node in order if node not in task.inputs and node not in outputs)
        # plan the memory for intermediate tensors
        buffer_bytes, buffer_offset = self.plan_memory(dag, order, require_allocate)

        # Construct the function body
        with FunctionBuilder(name='launch', kind='packed_func') as fb:
            # packed function arguments, packed_func(num_args: int32, arg_types: *int32, args: **void)
            num_args = scalar_var('num_args', 'int32')
            arg_types = Var('arg_types', ~int32)
            args = Var('args', ~void_pointer())
            fb.extend_params([num_args, arg_types, args])

            # extract the packed arguments
            node_map: Dict[TensorNode, Var] = {}  # tensor arguments
            scalar_map: Dict[Var, Var] = {}  # scalar arguments
            for idx, task_param in enumerate(updated_params):
                if isinstance(task_param, Var):
                    param = Var(task_param.name, task_param.type)
                    expect_type_code = ArgTypeCode.from_type(task_param.type).value
                    scalar_map[task_param] = param
                    init = deref(cast(args[idx], ~task_param.type))
                else:
                    assert isinstance(task_param, TensorNode)
                    param = Var(task_param.name, ~task_param.type.dtype)
                    expect_type_code = ArgTypeCode.POINTER.value
                    node_map[task_param] = param
                    init = cast(args[idx], param.type)
                fb += AssertStmt(
                    cond=(arg_types[idx] == expect_type_code),
                    msg='Argument {} expects a {}'.format(idx, ArgTypeCode(expect_type_code).name.lower()),
                )
                fb += DeclareStmt(param, init=init)

            # allocate memory space for intermediate tensors
            self.allocate_tensors(fb, device, buffer_bytes, buffer_offset, node_map)

            # schedule each tensor computation
            for node in order:
                if isinstance(node, TensorInput):
                    pass  # input tensor does not need scheduling, skip
                elif isinstance(node, GridCompute):
                    fb += self.schedule_grid_compute(node, node_map, scalar_map)
                else:
                    raise NotImplementedError()
        func = fb.get()
        self.ir_module.add(func.name, func)

        return self.ir_module

    def add_function(self, func: Function) -> Var:
        """Add a function to current ir module.

        This function is used to add a function to current ir module, which allows the
        schedule_grid_compute method calls a function to implement the given computation.

        Parameters
        ----------
        func: Function
            The function to be added.

        Returns
        -------
        ret: Var
            The variable points to the added function.
        """
        name = Namer.unique_name_among(func.name, self.ir_module.functions.keys())
        func.name = name
        self.ir_module.add(func.name, func)
        return self.ir_module.lookup_var(func.name)

    def grid_compute_params_and_args(
        self, node: GridCompute, node_map: Dict[TensorNode, Var], scalar_map: Dict[Var, Var]
    ) -> Tuple[List[Var], Dict[Union[TensorNode, Var], Var], List[Expr]]:
        # collect used tensors and scalars
        used_scalars: List[Var] = collect(node.value, Var)
        used_tensors: List[TensorNode] = collect(node.value, TensorNode, stop_when_found=True)

        # get the scalar and tensor parameters
        param_scalars: List[Var] = [v for v in used_scalars if v in scalar_map]
        param_tensors: List[TensorNode] = used_tensors + [node]

        # declare the parameter variables
        params: List[Var] = []
        # first declare the scalar parameters
        params.extend([Var(scalar.name, scalar.type) for scalar in param_scalars])
        param_map: Dict[Union[TensorNode, Var], Var] = {scalar: param for scalar, param in zip(param_scalars, params)}
        params.extend([Var(tensor.name, rewrite(tensor.type, param_map)) for tensor in param_tensors])
        # the tensor shape and layout may use the scalar parameters, so we need to rewrite them to use the param vars
        param_map.update({tensor: param for tensor, param in zip(param_tensors, params[len(param_scalars) :])})

        # construct the call arguments
        call_args: List[Expr] = []
        call_args.extend([scalar_map[param_scalar] for param_scalar in param_scalars])
        call_args.extend([node_map[param_tensor] for param_tensor in param_tensors])

        return params, param_map, call_args

    def schedule_grid_compute(
        self, node: GridCompute, node_map: Dict[TensorNode, Var], scalar_map: Dict[Var, Var]
    ) -> Stmt:
        raise NotImplementedError()


class ComputeExprLower(ExprRewriter, ComputeRewriter, TypeRewriter):
    def __init__(self, expr: Expr, param_map: Dict[Union[TensorNode, ScalarNode, Var], Expr]):
        super().__init__()
        self.sb: StmtBuilder = StmtBuilder()
        self.compute_expr: Expr = expr
        self.param_map: Dict[Union[TensorNode, ScalarNode], Expr] = param_map
        self.memo.update(param_map)

    def lower(self) -> Tuple[List[Stmt], Expr]:
        result = self.visit(self.compute_expr)
        assert len(self.sb.scope_stack) == 1, "some scope has not been exited?"
        return self.sb.scope_stack[0], result

    def visit_TensorInput(self, node: TensorInput):
        raise ValueError('Expect tensor input "{}" in param_map.'.format(node))

    def visit_ScalarInput(self, node: ScalarInput):
        raise ValueError('Expect scalar input "{}" in param_map.'.format(node))

    def visit_GridCompute(self, node: GridCompute):
        # declare intermediate tensor buffer
        buf = Var(node.name, node.type)

        # tensor compute loops
        for i in range(len(node.shape)):
            self.sb.enter_body(ForStmt(node.axes[i], self.visit(node.shape[i])))

        # at the innermost loop body
        expr = self.visit(node.value)
        self.sb.append(BufferStoreStmt(buf, node.axes, expr))

        # exit loop scope
        for i in range(len(node.shape)):
            self.sb.exit_body()
        return buf

    def visit_ReduceCompute(self, node: ReduceCompute):
        shape, axes, value = node.shape, node.axes, node.value
        # declare accumulator
        acc = scalar_var(node.name, infer_type(value))
        self.sb += DeclareStmt(acc, init=node.reduce_operation.initial_value(node.type))

        # reduction loops
        for i in range(len(shape)):
            self.sb.enter_body(ForStmt(axes[i], self.visit(shape[i])))

        # at the innermost loop body
        expr = self.visit(value)
        self.sb += AssignStmt(acc, node.reduce_operation.combine(acc, expr))

        # exit loop scope
        for i in range(len(shape)):
            self.sb.exit_body()

        # finalize
        acc = node.reduce_operation.finalize(acc, prod(shape))

        return acc

    def visit_ArgReduceCompute(self, node: ArgReduceCompute):
        extent, axis, value = self.visit(node.extent), node.axis, node.value
        value_dtype = infer_type(value)
        # declare index accumulator
        acc_index = scalar_var(node.name + '_idx', node.index_dtype)
        acc_value = scalar_var(node.name + '_val', value_dtype)

        # init accumulator
        self.sb += DeclareStmt(acc_index, init=convert(0))
        self.sb += DeclareStmt(acc_value, init=node.reduce_operation.initial_value(value_dtype))
        self.sb += AssignStmt(acc_index, 0)

        # reduction loops
        self.sb.enter_body(ForStmt(axis, extent))

        # compare and update index
        expr = self.visit(value)
        with self.sb.if_then(node.reduce_operation.arg_combine(lhs_value=expr, rhs_value=acc_value)):
            self.sb += AssignStmt(acc_value, expr)
            self.sb += AssignStmt(acc_index, axis)

        # exit loop
        self.sb.exit_body()

        return acc_index
