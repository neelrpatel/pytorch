# -*- coding: utf-8 -*-
# Owner(s): ["oncall: jit"]

from torch._C import _disabled_torch_function_impl
import torch.fx
import torch.nn.functional as F
from torch.testing._internal.common_utils import run_tests, TestCase, skipIfTorchDynamo, IS_WINDOWS
import unittest
import torch
import operator
import itertools
import io
from torch.utils._pytree import tree_map
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.experimental.symbolic_shapes import ShapeEnv, PySymInt, sym_float
from torch.utils._python_dispatch import TorchDispatchMode

aten = torch.ops.aten

try:
    import sympy
    # TODO(jansel): these tests fail on windows
    HAS_SYMPY = not IS_WINDOWS
except ImportError:
    HAS_SYMPY = False
skipIfNoSympy = unittest.skipIf(not HAS_SYMPY, "no sympy")


meta_funcs = {}


def register_meta(op):
    def decorator(f):
        def add_func(op):
            meta_funcs[op] = f
        tree_map(add_func, op)
        return f
    return decorator


@register_meta([aten.add.Tensor, aten.sub.Tensor])
def binary_meta(a, b):
    return a.new_empty(a.shape)


@register_meta(aten.cat.default)
def cat_meta(tensors, dim=0):
    concat_length = 0
    shape = tensors[0].shape
    for tensor in tensors:
        for idx, (common_length, length) in enumerate(zip(shape, tensor.shape)):
            if idx == dim:
                concat_length = concat_length + length
            else:
                assert length == common_length
    new_shape = list(shape)
    new_shape[dim] = concat_length
    return tensors[0].new_empty(new_shape)


@register_meta([aten.narrow_copy.default])
def narrow_copy_symint_meta(a, dim, start, length, **kwargs):
    shape = []
    for i, x in enumerate(a.shape):
        if i == dim:
            shape.append(length)
        else:
            shape.append(x)
    return a.new_empty(tuple(shape))


@register_meta([aten.expand.default])
def expand_symint_meta(a, size, implicit=False):
    return a.new_empty(size)


def create_contiguous(shape):
    strides = [1]
    for dim in reversed(shape[:-1]):
        strides.append(dim * strides[-1])
    return list(reversed(strides))


class FakeSymbolicTensor(torch.Tensor):
    @staticmethod
    def __new__(cls, sym_shape, sym_strides, dtype, layout, requires_grad, device, storage_offset=0):
        # TODO: this is wrong in general
        sym_stride = create_contiguous(sym_shape)
        r = torch.Tensor._make_wrapper_subclass(
            cls, sym_shape,
            sym_stride, storage_offset,
            dtype=dtype, layout=layout, requires_grad=requires_grad,
            device=device,
        )
        return r

    __torch_function__ = _disabled_torch_function_impl

    def new_empty(self, shape):
        return FakeSymbolicTensor(shape, None, self.dtype, self.layout, self.requires_grad, self.device)

    @classmethod
    def __torch_dispatch__(cls, func_overload, types, args=(), kwargs=None):
        if func_overload in meta_funcs:
            return meta_funcs[func_overload](*args, **kwargs)

        if func_overload == torch.ops.aten.new_empty.default:
            self = args[0]
            shape = args[1]
            return FakeSymbolicTensor(shape, self.stride(), self.dtype, self.layout, self.requires_grad, self.device)

        raise RuntimeError(f"operator {func_overload} not supported")


def create_symbolic_tensor(name, arg, shape_env, storage_offset=0):
    sym_shapes = tuple([shape_env.create_symint(f"{name}_{idx}", val) for idx, val in enumerate(arg.size())])
    sym_strides = tuple([shape_env.create_symint(f"{name}_{idx}_stride", val) for idx, val in enumerate(arg.stride())])
    return FakeSymbolicTensor(sym_shapes, sym_strides, arg.dtype, arg.layout, arg.requires_grad, arg.device, storage_offset)


CPP_SYMINT_CLASS = type(torch.SymIntNode.new_symint(1))


@skipIfTorchDynamo("Creating ShapeEnv fails for confusing reasons (also we never expect dynamo to see code like this)")
class TestPySymInt(TestCase):

    @skipIfNoSympy
    def test_arith_ops(self):
        shape_env = ShapeEnv()
        symints = []
        for i in range(5):
            symints.append((i, shape_env.create_symint(f"s{i}", i)))

        ops = [operator.add, operator.sub, operator.floordiv, operator.mul, operator.mod]

        for op in ops:
            for args in itertools.permutations(symints, 2):
                if not isinstance(args[0][1], int) and ((op != operator.mod or op != operator.floordiv) and args[1][0] != 0):
                    self.assertTrue(op(args[0][1], args[1][1]) == op(args[0][0], args[1][0]))


    @skipIfNoSympy
    def test_reverse_arith_ops(self):
        shape_env = ShapeEnv()

        a = shape_env.create_symint("s1", 2)
        self.assertTrue(5 // a == 5 // 2)

        a = shape_env.create_symint("s1", 2)
        self.assertTrue(5 * a == 5 * 2)


    @skipIfNoSympy
    def test_roundtrip(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)

        self.assertTrue(not isinstance(x.shape[0], PySymInt))
        self.assertTrue(isinstance(x.shape[0], CPP_SYMINT_CLASS))

        self.assertTrue(x.shape[0] == 5)
        self.assertTrue(x.shape[1] == 4)
        self.assertTrue(x.shape[2], 3)

        self.assertTrue(x.size()[0], 5)
        self.assertTrue(x.size()[1], 4)
        self.assertTrue(isinstance(x.size()[1], CPP_SYMINT_CLASS))
        self.assertTrue(x.size()[2] == 3)

        self.assertTrue(x.size(0) == 5)
        self.assertTrue(x.size(1) == 4)
        self.assertTrue(x.size(2) == 3)
        self.assertTrue(isinstance(x.size(2), CPP_SYMINT_CLASS))

        offset = shape_env.create_symint("offset", 2)
        y = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env, offset)
        self.assertTrue(isinstance(y.storage_offset(), CPP_SYMINT_CLASS))
        self.assertTrue(y.storage_offset() == 2)

        offset = 2
        z = create_symbolic_tensor("z", torch.randn(5, 4, 3), shape_env, offset)
        self.assertTrue(isinstance(z.storage_offset(), int))
        self.assertTrue(z.storage_offset() == 2)

    @skipIfNoSympy
    def test_binary(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(5, 4, 3), shape_env)

        z = x + y
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # broadcasting
        y = create_symbolic_tensor("y", torch.randn(1, 4, 1), shape_env)
        z = x + y
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

    @skipIfNoSympy
    def test_symint_args(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(5, 4, 1), shape_env)
        LAST_DIM = 2
        z = x.narrow_copy(LAST_DIM, 0, y.shape[LAST_DIM])
        self.assertTrue(z.shape[2] == int(y.shape[2]))

        # arithmetic expr with two symints
        z = x.narrow_copy(LAST_DIM, 0, x.shape[LAST_DIM] - y.shape[LAST_DIM])
        self.assertTrue(z.shape[2] == 2)

        # arithmetic expr with a symint and python int
        z = x.narrow_copy(LAST_DIM, 0, x.shape[LAST_DIM] - 1)
        self.assertTrue(z.shape[2] == 2)

    @skipIfNoSympy
    def test_symint_vargs(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(1, 4, 1), shape_env)

        # varargs
        z = y.expand(x.shape[0], y.shape[1], x.shape[2])
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # shape list
        z = y.expand((x.shape[0], y.shape[1], x.shape[2]))
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # mixed python symints and ints
        z = y.expand(x.shape[0], y.shape[1], 3)
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # mixed python symints and ints in a list
        z = y.expand((x.shape[0], y.shape[1], 3))
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # mixed python symints and ints
        z = y.expand(5, y.shape[1], x.shape[2])
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        # mixed python ints and symints in a list
        z = y.expand((5, y.shape[1], x.shape[2]))
        self.assertTrue(z.shape[0] == 5)
        self.assertTrue(z.shape[1] == 4)
        self.assertTrue(z.shape[2] == 3)

        z = y.expand((y.shape[1],))
        z = y.expand(y.shape[1])

    @skipIfNoSympy
    def test_stride(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 5), shape_env)
        self.assertIsInstance(x.stride()[0], CPP_SYMINT_CLASS)

    @skipIfNoSympy
    def test_size_expressions(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5), shape_env)
        expand_x = x.expand(x.shape[0], x.shape[0])
        if expand_x.shape[0] > 3:
            result = expand_x + expand_x
        else:
            result = expand_x + expand_x

        gt_op = shape_env.guards[0][0]
        self.assertTrue(isinstance(gt_op, sympy.core.relational.StrictGreaterThan))
        self.assertTrue(str(x.shape[0]), str(gt_op.args[0]))
        self.assertTrue(str(expand_x.shape[1]), str(x.shape[0]))
        self.assertTrue(str(expand_x.shape[1]), str(result.shape[0]))

    @skipIfNoSympy
    def test_int_to_float(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5), shape_env)
        r = sym_float(x.shape[0])
        self.assertTrue(isinstance(r, torch.SymFloatNode))

    @skipIfNoSympy
    def test_aten_ops(self):

        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5), shape_env)
        torch.ops.aten.narrow_copy.default(x, 0, 0, x.shape[0])

        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        torch.ops.aten.expand.default(x, [x.shape[0], x.shape[1], x.shape[2]])

    def test_fx_trace_intlist(self):
        class CustomModule(torch.nn.Module):
            def forward(self, x):
                bs, c, h, w = x.shape
                return F.pad(x, (0, w % 2, 0, h % 2, 0, 0))

        m = CustomModule()
        x = torch.rand(1, 3, 4, 4)
        # should not TypeError: pad(): argument 'pad' (position 2) must be
        # tuple of ints, not tuple
        torch.fx.symbolic_trace(m)

    @skipIfNoSympy
    def test_meta_symint(self):
        shape_env = ShapeEnv()
        a0 = shape_env.create_symint("a0", 2)
        r = torch.empty(a0, device='meta')
        self.assertIsInstance(r.shape[0], CPP_SYMINT_CLASS)

    @skipIfNoSympy
    def test_guard_int(self):
        shape_env = ShapeEnv()
        a0 = shape_env.create_symint("a0", 2)
        self.assertEqual(a0.guard_int(), 2)
        self.assertEqual(str(shape_env.guards[0][0]), "a0")
        self.assertEqual(shape_env.guards[0][1], 2)

    @skipIfNoSympy
    def test_int_conversion(self):
        shape_env = ShapeEnv()
        a0 = shape_env.create_symint("a0", 2)
        self.assertRaisesRegex(RuntimeError, "Trying to extract", lambda: int(a0))

    @skipIfNoSympy
    def test_symint_as_scalar(self):
        shape_env = ShapeEnv()
        a0 = shape_env.create_symint("a0", 2)

        sym_int_encountered = False

        class TestSymInt(TorchDispatchMode):
            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                assert func == torch.ops.aten.add.Tensor

                nonlocal sym_int_encountered
                sym_int_encountered = kwargs["alpha"] is a0
                kwargs["alpha"] = 0
                return func(*args)

        x = torch.rand([4, 4])
        with TestSymInt():
            y = torch.add(x, x, alpha=a0)

        self.assertTrue(sym_int_encountered)

    @skipIfNoSympy
    @unittest.mock.patch('sys.stdout', new_callable=io.StringIO)
    def test_print_readable_with_symints(self, mock_stdout):
        def f(a, b):
            dim0 = a.shape[0] + b.shape[0]
            dim1 = a.shape[1] + b.shape[1]
            d = a.new_empty(dim0, dim1)
            d = torch.ops.aten.native_dropout(d, 0.5, train=True)
            return d

        fx_g = make_fx(f, tracing_mode="symbolic")(torch.randn(5, 3), torch.randn(4, 3))
        fx_g.print_readable()

        self.assertExpectedInline(mock_stdout.getvalue().strip(), """\
class f(torch.nn.Module):
    def forward(self, a_1: f32[t0.size(0),t0.size(1)], b_1: f32[t1.size(0),t0.size(1)]):
        # No stacktrace found for following nodes
        sym_size: Sym(t0.size(0)) = torch.ops.aten.sym_size(a_1, 0)
        sym_size_1: Sym(t1.size(0)) = torch.ops.aten.sym_size(b_1, 0)
        add: Sym(t0.size(0) + t1.size(0)) = sym_size + sym_size_1;  sym_size = sym_size_1 = None
        sym_size_2: Sym(t0.size(1)) = torch.ops.aten.sym_size(a_1, 1)
        sym_size_3: Sym(t0.size(1)) = torch.ops.aten.sym_size(b_1, 1);  b_1 = None
        add_1: Sym(2*t0.size(1)) = sym_size_2 + sym_size_3;  sym_size_2 = sym_size_3 = None
        new_empty: f32[t0.size(0) + t1.size(0),2*t0.size(1)] = torch.ops.aten.new_empty.default(a_1, [add, add_1], dtype = torch.float32, layout = torch.strided, device = device(type='cpu'), pin_memory = False);  a_1 = add = add_1 = None
        native_dropout = torch.ops.aten.native_dropout.default(new_empty, 0.5, True);  new_empty = None
        getitem: f32[t0.size(0) + t1.size(0),2*t0.size(1)] = native_dropout[0]
        getitem_1: b8[t0.size(0) + t1.size(0),2*t0.size(1)] = native_dropout[1];  native_dropout = None
        return (getitem, getitem_1)""")  # noqa: B950


if __name__ == '__main__':
    run_tests()
