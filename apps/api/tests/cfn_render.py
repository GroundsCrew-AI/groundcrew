"""Render a CloudFormation template for given parameters, offline: load the YAML short forms
(!Ref, !Sub, ...) as their long forms, evaluate Conditions, and keep only the resources and
outputs that would exist. `resolve` substitutes concrete values (account, region, ARNs) so a
policy can be read or validated as JSON."""

import re
from pathlib import Path
from typing import Any

import yaml


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "Condition":
        return {"Condition": value}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {f"Fn::{suffix}": value}


_Loader.add_multi_constructor("!", _intrinsic)


def load(path: Path | str) -> dict[str, Any]:
    text = Path(path).read_text() if isinstance(path, Path) else path
    data: dict[str, Any] = yaml.load(text, _Loader)  # noqa: S506 (SafeLoader subclass)
    return data


def _param_values(template: dict[str, Any], params: dict[str, str]) -> dict[str, str]:
    values = {k: str(v.get("Default", "")) for k, v in template.get("Parameters", {}).items()}
    values.update(params)
    return values


def _value(node: Any, params: dict[str, str]) -> Any:
    if isinstance(node, dict) and set(node) == {"Ref"} and node["Ref"] in params:
        return params[node["Ref"]]
    return node


def _condition(node: Any, conditions: dict[str, Any], params: dict[str, str]) -> bool:
    if isinstance(node, dict) and len(node) == 1:
        ((fn, arg),) = node.items()
        if fn == "Condition":
            return _condition(conditions[arg], conditions, params)
        if fn == "Fn::Equals":
            a, b = (_value(x, params) for x in arg)
            return bool(a == b)
        if fn == "Fn::Not":
            return not _condition(arg[0], conditions, params)
        if fn == "Fn::And":
            return all(_condition(x, conditions, params) for x in arg)
        if fn == "Fn::Or":
            return any(_condition(x, conditions, params) for x in arg)
    raise ValueError(f"unsupported condition: {node!r}")


def render(template: dict[str, Any], params: dict[str, str] | None = None) -> dict[str, Any]:
    """{"Resources": ..., "Outputs": ...} that exist for these parameters (defaults otherwise),
    without their Condition keys."""
    values = _param_values(template, params or {})
    conditions = template.get("Conditions", {})
    on = {name: _condition(expr, conditions, values) for name, expr in conditions.items()}
    out: dict[str, Any] = {}
    for section in ("Resources", "Outputs"):
        kept = {}
        for name, body in template.get(section, {}).items():
            if "Condition" in body and not on[body["Condition"]]:
                continue
            kept[name] = {k: v for k, v in body.items() if k != "Condition"}
        out[section] = kept
    return out


def resolve(node: Any, values: dict[str, str], attrs: dict[str, str]) -> Any:
    """Replace Ref / Sub / GetAtt with concrete strings: `values` holds parameters and pseudo
    parameters (AWS::AccountId, ...), `attrs` holds "Logical.Attribute" and logical-id Refs."""
    if isinstance(node, list):
        return [resolve(x, values, attrs) for x in node]
    if not isinstance(node, dict):
        return node
    if set(node) == {"Ref"}:
        name = node["Ref"]
        return values.get(name) or attrs[name]
    if set(node) == {"Fn::GetAtt"}:
        return attrs[".".join(node["Fn::GetAtt"])]
    if set(node) == {"Fn::Sub"}:
        return re.sub(
            r"\$\{([^}]+)\}", lambda m: values.get(m.group(1)) or attrs[m.group(1)], node["Fn::Sub"]
        )
    return {k: resolve(v, values, attrs) for k, v in node.items()}
