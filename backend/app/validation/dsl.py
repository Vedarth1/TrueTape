# backend/app/validation/dsl.py
"""Pure evaluator for the rule DSL. A condition tree plus one record's values
in, a three-state verdict out. No DB access, no ORM imports.

Kept pure for two reasons. It is unit-testable without Postgres; and it is the
only part of the engine an AI-authored rule can reach, so it must be provably
side-effect-free -- the tree is WALKED, never eval'd or exec'd, which is the
entire security argument for the rule-authoring feature (validation.py:41).

THREE-VALUED LOGIC. The DSL has True / False / ABSENT, not True / False.
'not_applicable' is the correct answer for most rules against a
document_manifest row, and collapsing it into False would turn every column the
manifest simply does not have into a CRITICAL exception -- 842 of them.
"""
from datetime import date, datetime, time, timezone


class _Absent:
    """Sentinel: 'this operand has no value here'. Distinct from None.

    __bool__ raises on purpose. Every `if value:` written against an ABSENT
    operand is a three-valued-logic bug, and a loud TypeError in a test beats a
    rule that silently never fires against real data.
    """
    __slots__ = ()

    def __repr__(self):
        return "ABSENT"

    def __bool__(self):
        raise TypeError("ABSENT has no truth value; branch on `is ABSENT` first")


ABSENT = _Absent()


class EvalContext:
    """Everything one row-scope evaluation is allowed to see."""

    def __init__(self, data, field_errors, in_scope, now,
                 source_system=None, batch_id=None):
        # `or {}` on both: nullable JSONB persists Python None as the JSONB null
        # token, so these read back as None rather than {} on clean rows.
        self.data = data or {}
        self.field_errors = field_errors or {}
        # The canonical fields the SOURCE FILE actually declared a column for.
        # This is what separates "the manifest has no principal column"
        # (not_applicable) from "the loan tape's principal cell is blank" (fail).
        self.in_scope = in_scope or frozenset()
        self.now = now
        self.source_system = source_system
        self.batch_id = batch_id
        self.referenced = set()      # fields touched, for details / field_name


_COMPARE = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}

_ARITH = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
}


def _to_naive_utc(v):
    """date / datetime / ISO string -> naive-UTC datetime, or None if unparseable.

    Normalised to naive UTC rather than left as-is because `now` is tz-aware
    (audit convention) while `data` holds whatever the generator's .isoformat()
    produced. Subtracting an aware from a naive datetime is a TypeError, and
    stripping tzinfo without converting would silently shift by the offset.
    """
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime.combine(v, time.min)
    elif isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# --- functions -------------------------------------------------------------
# Each takes the raw arg NODES plus ctx, and evaluates what it needs itself.
# not_null and field_error need the node rather than its value: see below.

def _fn_not_null(args, ctx):
    """Presence test. Deliberately does NOT inherit ABSENT from a field node.

    This is what makes REQUIRED_CORE_FIELDS work. If not_null just read the
    field's value it would inherit ABSENT for a blank cell and return
    not_applicable -- the exact defect the rule exists to catch. So it inspects
    scope directly:
        column absent from the file  -> ABSENT (not_applicable)
        column present, cell blank   -> False  (fail: genuinely missing)
        column present, value bad    -> True   (present; INVALID_* owns format,
                                                so one bad cell is not counted
                                                twice as two exceptions)
    """
    node = args[0]
    if node.get("type") != "field":
        v = _eval(node, ctx)
        return ABSENT if v is ABSENT else v is not None
    name = node["name"]
    ctx.referenced.add(name)
    if name not in ctx.in_scope:
        return ABSENT
    if name in ctx.field_errors:
        return True
    v = ctx.data.get(name, ABSENT)
    return not (v is ABSENT or v is None)


def _fn_field_error(args, ctx):
    """True when normalization recorded a coercion failure for this field.

    Same scope-first treatment as not_null, and for a sharper reason:
    _normalize_row puts a failed coercion in field_errors and OMITS it from
    `data` (normalizer.py:44-46). A value-based implementation would therefore
    see ABSENT and make INVALID_DATE_FORMAT unable to fire on precisely the
    rows it targets.
    """
    node = args[0]
    if node.get("type") != "field":
        raise ValueError("field_error() takes a field node")
    name = node["name"]
    ctx.referenced.add(name)
    if name not in ctx.in_scope:
        return ABSENT
    return name in ctx.field_errors


def _fn_in_set(args, ctx):
    v = _eval(args[0], ctx)
    members = _eval(args[1], ctx)
    if v is ABSENT or members is ABSENT or v is None:
        return ABSENT
    return v in (members or [])


def _fn_upper(args, ctx):
    v = _eval(args[0], ctx)
    return ABSENT if (v is ABSENT or v is None) else str(v).upper()


def _fn_lower(args, ctx):
    v = _eval(args[0], ctx)
    return ABSENT if (v is ABSENT or v is None) else str(v).lower()


def _fn_days_between(args, ctx):
    """Signed whole days: days_between(now, x) is x's age in days."""
    a = _eval(args[0], ctx)
    b = _eval(args[1], ctx)
    if a is ABSENT or b is ABSENT:
        return ABSENT
    a, b = _to_naive_utc(a), _to_naive_utc(b)
    if a is None or b is None:
        return ABSENT
    return (a - b).days


_FUNCS = {
    "not_null": _fn_not_null,
    "field_error": _fn_field_error,
    "in_set": _fn_in_set,
    "upper": _fn_upper,
    "lower": _fn_lower,
    "days_between": _fn_days_between,
}


# --- the walker ------------------------------------------------------------

def _eval(node, ctx):
    if not isinstance(node, dict):
        raise ValueError(f"malformed DSL node: {node!r}")

    t = node.get("type")

    if t == "literal":
        return node["value"]

    if t == "field":
        name = node["name"]
        ctx.referenced.add(name)
        if name not in ctx.in_scope:
            return ABSENT        # the source never declared this column
        if name in ctx.field_errors:
            return ABSENT        # unusable value; only field_error() may see it
        return ctx.data.get(name, ABSENT)

    if t == "context":
        name = node["name"]
        if name == "now":
            return ctx.now
        if name == "source_system":
            return ctx.source_system if ctx.source_system is not None else ABSENT
        if name == "batch_id":
            return ctx.batch_id if ctx.batch_id is not None else ABSENT
        raise ValueError(f"unknown context: {name}")

    if t == "func":
        fn = _FUNCS.get(node["name"])
        if fn is None:
            raise ValueError(f"unknown function: {node['name']}")
        return fn(node.get("args") or [], ctx)

    if t == "comparison":
        left = _eval(node["left"], ctx)
        right = _eval(node["right"], ctx)
        if left is ABSENT or right is ABSENT or left is None or right is None:
            return ABSENT
        op = _COMPARE.get(node["operator"])
        if op is None:
            raise ValueError(f"unknown comparison operator: {node['operator']}")
        try:
            # ISO date strings order lexicographically, which is exactly why
            # MATURITY_AFTER_ORIGINATION needs no date parsing at all.
            return op(left, right)
        except TypeError:
            return ABSENT        # incomparable types: a data problem, not a verdict

    if t == "arith":
        left = _eval(node["left"], ctx)
        right = _eval(node["right"], ctx)
        if left is ABSENT or right is ABSENT or left is None or right is None:
            return ABSENT
        op = _ARITH.get(node["operator"])
        if op is None:
            raise ValueError(f"unknown arithmetic operator: {node['operator']}")
        try:
            return op(left, right)
        except (TypeError, ZeroDivisionError):
            return ABSENT

    if t == "not":
        v = _eval(node["operand"], ctx)
        return ABSENT if v is ABSENT else (not v)

    if t == "and":
        # Kleene: False dominates. ABSENT survives only if nothing is False.
        # The `is ABSENT` test comes first, so the truthiness check below can
        # never trip _Absent.__bool__.
        saw_absent = False
        for operand in node["operands"]:
            v = _eval(operand, ctx)
            if v is ABSENT:
                saw_absent = True
            elif not v:
                return False
        return ABSENT if saw_absent else True

    if t == "or":
        # Kleene: True dominates. This is what makes
        # PAYMENT_STATUS_DPD_CONSISTENT *pass* on a paid_off row with no
        # days_past_due, instead of degrading to not_applicable: the rule is
        # vacuously satisfied because the status is not 'current'.
        saw_absent = False
        for operand in node["operands"]:
            v = _eval(operand, ctx)
            if v is ABSENT:
                saw_absent = True
            elif v:
                return True
        return ABSENT if saw_absent else False

    raise ValueError(f"unknown DSL node type: {t!r}")


def evaluate(condition, ctx):
    """Walk one condition tree -> ('pass'|'fail'|'not_applicable', details).

    Every seed rule's `condition` is written as the PASS predicate -- the thing
    that must hold -- so True means the row is fine and False is the exception.
    No inversion anywhere.
    """
    verdict = _eval(condition, ctx)          # must test `is ABSENT` before any
                                             # truthiness: __bool__ raises.
    refd = sorted(ctx.referenced)
    details = {
        "referenced_fields": refd,
        "values": {f: ctx.data[f] for f in refd if f in ctx.data},
        "field_errors": {f: ctx.field_errors[f] for f in refd
                         if f in ctx.field_errors},
        "out_of_scope": [f for f in refd if f not in ctx.in_scope],
    }
    if verdict is ABSENT:
        return "not_applicable", details
    return ("pass" if verdict else "fail"), details


class _SafeDict(dict):
    def __missing__(self, key):
        return "—"


def render_message(template, values):
    """message_template.format() that cannot KeyError on a missing operand."""
    try:
        return template.format_map(_SafeDict(values))
    except (IndexError, ValueError):
        return template
