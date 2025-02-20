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
from typing import Dict

from hidet.ir.builders import FunctionBuilder
from hidet.ir.compute import TensorNode, GridCompute
from hidet.ir.expr import Var, convert, call
from hidet.ir.tools import rewrite
from hidet.ir.stmt import Stmt, BufferStoreStmt, EvaluateStmt
from ..auto_scheduler import AutoScheduler, ComputeExprLower


class CpuAutoScheduler(AutoScheduler):
    def schedule_grid_compute(
        self, node: GridCompute, node_map: Dict[TensorNode, Var], scalar_map: Dict[Var, Var]
    ) -> Stmt:
        # pylint: disable=too-many-locals, import-outside-toplevel, unnecessary-comprehension
        from hidet.ir.mapping import row_repeat, TaskMapping

        params, param_map, call_args = self.grid_compute_params_and_args(node, node_map, scalar_map)

        with FunctionBuilder(name=f'compute_{node.name}', kind='host_kernel') as fb:
            # set function parameters
            fb.extend_params(params)

            mapping: TaskMapping = row_repeat(*[rewrite(d, param_map) for d in node.shape])
            iter_names = [f'i{i}' for i in range(len(node.shape))]
            with fb.for_mapping(iter_names, mapping, convert(0)) as task_index:
                out_param: Var = param_map[node]
                compute_lower = ComputeExprLower(node.value, param_map=param_map)
                stmts, value = compute_lower.lower()
                rmap = {axis: axis_value for axis, axis_value in zip(node.axes, task_index)}
                stmts, value = [rewrite(stmt, rmap) for stmt in stmts], rewrite(value, rmap)
                fb += stmts
                fb += BufferStoreStmt(out_param, task_index, value)
        func = fb.get()
        func_var = self.add_function(func)

        # call the created function in the launch function
        return EvaluateStmt(call(func_var, args=call_args))
