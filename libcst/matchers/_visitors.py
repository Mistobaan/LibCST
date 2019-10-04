# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict
from inspect import ismethod, signature
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
    get_type_hints,
)

import libcst as cst
from libcst import CSTTransformer, CSTVisitor
from libcst.matchers._decorators import (
    CONSTRUCTED_LEAVE_MATCHER_ATTR,
    CONSTRUCTED_VISIT_MATCHER_ATTR,
    VISIT_NEGATIVE_MATCHER_ATTR,
    VISIT_POSITIVE_MATCHER_ATTR,
)
from libcst.matchers._matcher_base import (
    AllOf,
    AtLeastN,
    AtMostN,
    BaseMatcherNode,
    MatchIfTrue,
    OneOf,
    matches,
)


_CSTNodeT = TypeVar("_CSTNodeT", bound=cst.CSTNode)


class MatchDecoratorMismatch(Exception):
    # pyre-ignore We don't care about the type of func, just that its callable.
    def __init__(self, func: Callable[..., Any], message: str) -> None:
        super().__init__(
            # pyre-ignore Pyre doesn't believe functions have __qualname__
            f"Invalid function signature for {func.__qualname__}: {message}"
        )


def _get_possible_match_classes(matcher: BaseMatcherNode) -> List[Type[cst.CSTNode]]:
    if isinstance(matcher, (OneOf, AllOf)):
        return [getattr(cst, m.__class__.__name__) for m in matcher.options]
    else:
        return [getattr(cst, matcher.__class__.__name__)]


def _get_possible_annotated_classes(annotation: Type[object]) -> List[Type[object]]:
    if getattr(annotation, "__origin__", None) is Union:
        return getattr(annotation, "__args__", [])
    else:
        return [annotation]


def _get_valid_leave_annotations_for_classes(
    classes: Sequence[Type[cst.CSTNode]]
) -> Set[Type[object]]:
    retval: Set[Type[object]] = set()

    for cls in classes:
        # Look up the leave annotation for each class, combine them so we get a list of
        # all possible valid return annotations. Its not really possible for us (or
        # pyre) to fully enforce return types given the presence of OneOf/AllOf matchers, so
        # we do the best we can by taking a union of all valid return annotations.

        # TODO: We could possibly teach LibCST codegen to generate a mapping of class
        # to valid leave annotations when it generates leave_<Node> methods, but for
        # now this is functionally identical. That would get rid of the need to provide
        # a namespace here, as well as a gross getattr on the CSTTransformer class.
        meth = getattr(cst.CSTTransformer, f"leave_{cls.__name__}")
        namespace: Dict[str, object] = {
            **{x: getattr(cst, x) for x in dir(cst)},
            "cst": cst,
            "libcst": cst,
        }
        type_hints = get_type_hints(meth, namespace, namespace)
        if "return" in type_hints:
            retval.update(_get_possible_annotated_classes(type_hints["return"]))

    return retval


def _verify_return_annotation(
    possible_match_classes: Sequence[Type[cst.CSTNode]],
    # pyre-ignore We only care that meth is callable.
    meth: Callable[..., Any],
    decorator_name: str,
    *,
    expected_none: bool,
) -> None:
    type_hints = get_type_hints(meth)
    if expected_none:
        # Simply look for any annotation at all and if it exists, verify that
        # it is "None".
        if type_hints.get("return", type(None)) is not type(None):  # noqa: E721
            raise MatchDecoratorMismatch(
                meth,
                f"@{decorator_name} should only decorate functions that do "
                + "not return.",
            )
    else:
        if "return" not in type_hints:
            # Can't check this, type annotation not supplied.
            return

        possible_annotated_classes = _get_possible_annotated_classes(
            type_hints["return"]
        )
        possible_returns = _get_valid_leave_annotations_for_classes(
            possible_match_classes
        )

        # Look at the union of specified return annotation, make sure that
        # they are all subclasses of the original leave_<Node> return
        # annotations. This catches when somebody tries to return a new node
        # that we know can't fit where the existing node was in the tree.
        for ret in possible_annotated_classes:
            for annotation in possible_returns:
                if issubclass(ret, annotation):
                    # This annotation is a superclass of the possible match,
                    # so we know that the types are correct.
                    break
            else:
                # The current ret was not a subclass of any of the annotated
                # return types.
                raise MatchDecoratorMismatch(
                    meth,
                    f"@{decorator_name} decorated function cannot return "
                    + f"the type {ret.__name__}.",
                )
                pass


def _verify_parameter_annotations(
    possible_match_classes: Sequence[Type[cst.CSTNode]],
    # pyre-ignore We only care that meth is callable.
    meth: Callable[..., Any],
    decorator_name: str,
    *,
    expected_param_count: int,
) -> None:
    # First, verify that the number of parameters is sane.
    meth_signature = signature(meth)
    if len(meth_signature.parameters) != expected_param_count:
        raise MatchDecoratorMismatch(
            meth,
            f"@{decorator_name} should decorate functions which take "
            + f"{expected_param_count} parameter"
            + ("s" if expected_param_count > 1 else ""),
        )

    # Finally, for each parameter, make sure that the annotation includes
    # each of the classes that might appear given the match string. This
    # can be done in the simple case by just specifying the correct cst node
    # type. For complex matches that use OneOf/AllOf, this could be a base class
    # that encompases all possible matches, or a union.
    params = [v for k, v in get_type_hints(meth).items() if k != "return"]
    for param in params:
        # Go through each possible matcher, and make sure that the annotation
        # for types is a superclass of each matcher.
        possible_annotated_classes = _get_possible_annotated_classes(param)
        for match in possible_match_classes:
            for annotation in possible_annotated_classes:
                if issubclass(match, annotation):
                    # This annotation is a superclass of the possible match,
                    # so we know that the types are correct.
                    break
            else:
                # The current match was not a subclass of any of the annotated
                # types.
                raise MatchDecoratorMismatch(
                    meth,
                    f"@{decorator_name} can be called with {match.__name__} "
                    + f"but the decorated function parameter annotations do "
                    + f"not include this type.",
                )


def _check_types(
    # pyre-ignore We don't care about the type of sequence, just that its callable.
    decoratormap: Dict[BaseMatcherNode, Sequence[Callable[..., Any]]],
    decorator_name: str,
    *,
    expected_param_count: int,
    expected_none_return: bool,
) -> None:
    for matcher, methods in decoratormap.items():
        # Given the matcher class we have, get the list of possible cst nodes that
        # could be passed to the functionis we wrap.
        possible_match_classes = _get_possible_match_classes(matcher)
        has_invalid_top_level = any(
            isinstance(m, (AtLeastN, AtMostN, MatchIfTrue))
            for m in possible_match_classes
        )

        # Now, loop through each function we wrap and verify that the type signature
        # is valid.
        for meth in methods:
            # First thing first, make sure this isn't wrapping an inner class.
            if not ismethod(meth):
                raise MatchDecoratorMismatch(
                    meth,
                    "Matcher decorators should only be used on methods of "
                    + "MatcherDecoratableTransformer or "
                    + "MatcherDecoratableVisitor",
                )
            if has_invalid_top_level:
                raise MatchDecoratorMismatch(
                    meth,
                    "The root matcher in a matcher decorator cannot be an "
                    + "AtLeastN, AtMostN or MatchIfTrue matcher",
                )

            # Now, check that the return annotation is valid.
            _verify_return_annotation(
                possible_match_classes,
                meth,
                decorator_name,
                expected_none=expected_none_return,
            )

            # Finally, check that the parameter annotations are valid.
            _verify_parameter_annotations(
                possible_match_classes,
                meth,
                decorator_name,
                expected_param_count=expected_param_count,
            )


def _gather_matchers(obj: object) -> Set[BaseMatcherNode]:
    visit_matchers: Set[BaseMatcherNode] = set()

    for func in dir(obj):
        try:
            for matcher in getattr(getattr(obj, func), VISIT_POSITIVE_MATCHER_ATTR, []):
                visit_matchers.add(cast(BaseMatcherNode, matcher))
            for matcher in getattr(getattr(obj, func), VISIT_NEGATIVE_MATCHER_ATTR, []):
                visit_matchers.add(cast(BaseMatcherNode, matcher))
        except Exception:
            # This could be a caculated property, and calling getattr() evaluates it.
            # We have no control over the implementation detail, so if it raises, we
            # should not crash.
            pass

    return visit_matchers


def _gather_constructed_visit_funcs(
    obj: object
) -> Dict[BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]]:
    constructed_visitors: Dict[
        BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]
    ] = {}

    for funcname in dir(obj):
        try:
            func = cast(Callable[[cst.CSTNode], None], getattr(obj, funcname))
        except Exception:
            # This could be a caculated property, and calling getattr() evaluates it.
            # We have no control over the implementation detail, so if it raises, we
            # should not crash.
            continue
        for matcher in getattr(func, CONSTRUCTED_VISIT_MATCHER_ATTR, []):
            casted_matcher = cast(BaseMatcherNode, matcher)
            constructed_visitors[casted_matcher] = (
                *constructed_visitors.get(casted_matcher, ()),
                func,
            )

    return constructed_visitors


# pyre-ignore: There is no reasonable way to type this, so ignore the Any type. This
# is because the leave_* methods have a different signature depending on whether they
# are in a MatcherDecoratableTransformer or a MatcherDecoratableVisitor.
def _gather_constructed_leave_funcs(
    obj: object
) -> Dict[BaseMatcherNode, Sequence[Callable[..., Any]]]:
    constructed_visitors: Dict[
        BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]
    ] = {}

    for funcname in dir(obj):
        try:
            func = cast(Callable[[cst.CSTNode], None], getattr(obj, funcname))
        except Exception:
            # This could be a caculated property, and calling getattr() evaluates it.
            # We have no control over the implementation detail, so if it raises, we
            # should not crash.
            continue
        for matcher in getattr(func, CONSTRUCTED_LEAVE_MATCHER_ATTR, []):
            casted_matcher = cast(BaseMatcherNode, matcher)
            constructed_visitors[casted_matcher] = (
                *constructed_visitors.get(casted_matcher, ()),
                func,
            )

    return constructed_visitors


def _visit_matchers(
    matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]], node: cst.CSTNode
) -> Dict[BaseMatcherNode, Optional[cst.CSTNode]]:
    new_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]] = {}
    for matcher, existing_node in matchers.items():
        # We don't care about visiting matchers that are already true.
        if existing_node is None and matches(node, matcher):
            # This node matches! Remember which node it was so we can
            # cancel it later.
            new_matchers[matcher] = node
        else:
            new_matchers[matcher] = existing_node
    return new_matchers


def _leave_matchers(
    matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]], node: cst.CSTNode
) -> Dict[BaseMatcherNode, Optional[cst.CSTNode]]:
    new_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]] = {}
    for matcher, existing_node in matchers.items():
        if node is existing_node:
            # This node matches, so we are no longer inside it.
            new_matchers[matcher] = None
        else:
            # We aren't leaving this node.
            new_matchers[matcher] = existing_node
    return new_matchers


def _all_positive_matchers_true(
    all_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]], obj: object
) -> bool:
    requested_matchers = getattr(obj, VISIT_POSITIVE_MATCHER_ATTR, [])
    for matcher in requested_matchers:
        if all_matchers[matcher] is None:
            # The passed in object has been decorated with a matcher that isn't
            # active.
            return False
    return True


def _all_negative_matchers_false(
    all_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]], obj: object
) -> bool:
    requested_matchers = getattr(obj, VISIT_NEGATIVE_MATCHER_ATTR, [])
    for matcher in requested_matchers:
        if all_matchers[matcher] is not None:
            # The passed in object has been decorated with a matcher that is active.
            return False
    return True


def _should_allow_visit(
    all_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]], obj: object
) -> bool:
    return _all_positive_matchers_true(
        all_matchers, obj
    ) and _all_negative_matchers_false(all_matchers, obj)


def _visit_constructed_funcs(
    visit_funcs: Dict[BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]],
    all_matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]],
    node: cst.CSTNode,
) -> None:
    for matcher, visit_funcs in visit_funcs.items():
        if matches(node, matcher):
            for visit_func in visit_funcs:
                if _should_allow_visit(all_matchers, visit_func):
                    visit_func(node)


class MatcherDecoratableTransformer(CSTTransformer):
    """
    This class provides all of the features of a :class:`libcst.CSTTransformer`, and
    additionally supports various decorators to control when methods get called when
    traversing a tree. Use this instead of a :class:`libcst.CSTTransformer` if you
    wish to do more powerful decorator-based visiting.
    """

    def __init__(self) -> None:
        CSTTransformer.__init__(self)
        # List of gating matchers that we need to track and evaluate. We use these
        # in conjuction with the call_if_inside and call_if_not_inside decorators
        # to determine whether or not to call a visit/leave function.
        self._matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]] = {
            m: None for m in _gather_matchers(self)
        }
        # Mapping of matchers to functions. If in the course of visiting the tree,
        # a node matches one of these matchers, the corresponding function will be
        # called as if it was a visit_* method.
        self._extra_visit_funcs: Dict[
            BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]
        ] = _gather_constructed_visit_funcs(self)
        # Mapping of matchers to functions. If in the course of leaving the tree,
        # a node matches one of these matchers, the corresponding function will be
        # called as if it was a leave_* method.
        self._extra_leave_funcs: Dict[
            BaseMatcherNode,
            Sequence[
                Callable[
                    [cst.CSTNode, cst.CSTNode], Union[cst.CSTNode, cst.RemovalSentinel]
                ]
            ],
        ] = _gather_constructed_leave_funcs(self)
        # Make sure visit/leave functions constructed with @visit and @leave decorators
        # have correct type annotations.
        _check_types(
            self._extra_visit_funcs,
            "visit",
            expected_param_count=1,
            expected_none_return=True,
        )
        _check_types(
            self._extra_leave_funcs,
            "leave",
            expected_param_count=2,
            expected_none_return=False,
        )

    def on_visit(self, node: cst.CSTNode) -> bool:
        # First, evaluate any matchers that we have which we are not inside already.
        self._matchers = _visit_matchers(self._matchers, node)

        # Now, call any visitors that were hooked using a visit decorator.
        _visit_constructed_funcs(self._extra_visit_funcs, self._matchers, node)

        # Now, evaluate whether this current function has any matchers it requires.
        if not _should_allow_visit(
            self._matchers, getattr(self, f"visit_{type(node).__name__}", None)
        ):
            # We shouldn't visit this directly. However, we should continue
            # visiting its children.
            return True

        # Either the visit_func doesn't exist, we have no matchers, or we passed all
        # matchers. In either case, just call the superclass behavior.
        return CSTTransformer.on_visit(self, node)

    def on_leave(
        self, original_node: _CSTNodeT, updated_node: _CSTNodeT
    ) -> Union[_CSTNodeT, cst.RemovalSentinel]:
        # First, evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers, getattr(self, f"leave_{type(original_node).__name__}", None)
        ):
            retval = CSTTransformer.on_leave(self, original_node, updated_node)
        else:
            retval = updated_node

        # Now, call any visitors that were hooked using a leave decorator.
        for matcher, leave_funcs in reversed(list(self._extra_leave_funcs.items())):
            if not matches(original_node, matcher):
                continue
            for leave_func in leave_funcs:
                if _should_allow_visit(self._matchers, leave_func) and isinstance(
                    retval, cst.CSTNode
                ):
                    retval = leave_func(original_node, retval)

        # Now, see if we have any matchers we should deactivate.
        self._matchers = _leave_matchers(self._matchers, original_node)

        # pyre-ignore The return value of on_leave is subtly wrong in that we can
        # actually return any value that passes this node's parent's constructor
        # validation. Fixing this is beyond the scope of this file, and would involve
        # forcing a lot of ensure_type() checks across the codebase.
        return retval

    def on_visit_attribute(self, node: cst.CSTNode, attribute: str) -> None:
        # Evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers,
            getattr(self, f"visit_{type(node).__name__}_{attribute}", None),
        ):
            # Either the visit_func doesn't exist, we have no matchers, or we passed all
            # matchers. In either case, just call the superclass behavior.
            return CSTVisitor.on_visit_attribute(self, node, attribute)

    def on_leave_attribute(self, original_node: cst.CSTNode, attribute: str) -> None:
        # Evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers,
            getattr(self, f"leave_{type(original_node).__name__}_{attribute}", None),
        ):
            # Either the visit_func doesn't exist, we have no matchers, or we passed all
            # matchers. In either case, just call the superclass behavior.
            CSTVisitor.on_leave_attribute(self, original_node, attribute)

    def _transform_module_impl(self, tree: cst.Module) -> cst.Module:
        return tree.visit(self)


class MatcherDecoratableVisitor(CSTVisitor):
    """
    This class provides all of the features of a :class:`libcst.CSTVisitor`, and
    additionally supports various decorators to control when methods get called
    when traversing a tree. Use this instead of a :class:`libcst.CSTVisitor` if
    you wish to do more powerful decorator-based visiting.
    """

    def __init__(self) -> None:
        CSTVisitor.__init__(self)
        # List of gating matchers that we need to track and evaluate. We use these
        # in conjuction with the call_if_inside and call_if_not_inside decorators
        # to determine whether or not to call a visit/leave function.
        self._matchers: Dict[BaseMatcherNode, Optional[cst.CSTNode]] = {
            m: None for m in _gather_matchers(self)
        }
        # Mapping of matchers to functions. If in the course of visiting the tree,
        # a node matches one of these matchers, the corresponding function will be
        # called as if it was a visit_* method.
        self._extra_visit_funcs: Dict[
            BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]
        ] = _gather_constructed_visit_funcs(self)
        # Mapping of matchers to functions. If in the course of leaving the tree,
        # a node matches one of these matchers, the corresponding function will be
        # called as if it was a leave_* method.
        self._extra_leave_funcs: Dict[
            BaseMatcherNode, Sequence[Callable[[cst.CSTNode], None]]
        ] = _gather_constructed_leave_funcs(self)
        # Make sure visit/leave functions constructed with @visit and @leave decorators
        # have correct type annotations.
        _check_types(
            self._extra_visit_funcs,
            "visit",
            expected_param_count=1,
            expected_none_return=True,
        )
        _check_types(
            self._extra_leave_funcs,
            "leave",
            expected_param_count=1,
            expected_none_return=True,
        )

    def on_visit(self, node: cst.CSTNode) -> bool:
        # First, evaluate any matchers that we have which we are not inside already.
        self._matchers = _visit_matchers(self._matchers, node)

        # Now, call any visitors that were hooked using a visit decorator.
        _visit_constructed_funcs(self._extra_visit_funcs, self._matchers, node)

        # Now, evaluate whether this current function has a decorator on it.
        if not _should_allow_visit(
            self._matchers, getattr(self, f"visit_{type(node).__name__}", None)
        ):
            # We shouldn't visit this directly. However, we should continue
            # visiting its children.
            return True

        # Either the visit_func doesn't exist, we have no matchers, or we passed all
        # matchers. In either case, just call the superclass behavior.
        return CSTVisitor.on_visit(self, node)

    def on_leave(self, original_node: cst.CSTNode) -> None:
        # First, evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers, getattr(self, f"leave_{type(original_node).__name__}", None)
        ):
            CSTVisitor.on_leave(self, original_node)

        # Now, call any visitors that were hooked using a leave decorator.
        for matcher, leave_funcs in reversed(list(self._extra_leave_funcs.items())):
            if not matches(original_node, matcher):
                continue
            for leave_func in leave_funcs:
                if _should_allow_visit(self._matchers, leave_func):
                    leave_func(original_node)

        # Now, see if we have any matchers we should deactivate.
        self._matchers = _leave_matchers(self._matchers, original_node)

    def on_visit_attribute(self, node: cst.CSTNode, attribute: str) -> None:
        # Evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers,
            getattr(self, f"visit_{type(node).__name__}_{attribute}", None),
        ):
            # Either the visit_func doesn't exist, we have no matchers, or we passed all
            # matchers. In either case, just call the superclass behavior.
            return CSTVisitor.on_visit_attribute(self, node, attribute)

    def on_leave_attribute(self, original_node: cst.CSTNode, attribute: str) -> None:
        # Evaluate whether this current function has a decorator on it.
        if _should_allow_visit(
            self._matchers,
            getattr(self, f"leave_{type(original_node).__name__}_{attribute}", None),
        ):
            # Either the visit_func doesn't exist, we have no matchers, or we passed all
            # matchers. In either case, just call the superclass behavior.
            CSTVisitor.on_leave_attribute(self, original_node, attribute)
