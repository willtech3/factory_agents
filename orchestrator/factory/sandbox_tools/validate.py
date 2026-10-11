#!/usr/bin/env python3
"""Validate a factory artifact against its JSON schema, from inside a sandbox.

Uploaded to every session at /mnt/session/uploads/tools/validate.py together with
the schemas directory. Agents run it before finishing so schema errors are caught
by the author rather than by the orchestrator. Dependency-free on purpose: the
sandbox may not have `jsonschema` installed, so this performs a structural check
(required keys, types, enums, patterns, min/max items) covering what the factory
schemas actually use. The orchestrator re-validates with the full `jsonschema`
library on receipt.

Usage: python -I validate.py <artifact.json> [--schema-dir /mnt/session/uploads/schemas]
Exit code 0 on success, 1 on validation errors, 2 on usage errors.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ARTIFACT_TO_SCHEMA = {
    "outcome": "outcome.schema.json",
    "decomposition": "decomposition.schema.json",
    "spec": "spec.schema.json",
    "verdict": "judge_verdict.schema.json",
    "implementation_report": "implementation_report.schema.json",
    "findings": "review_findings.schema.json",
    "review_response": "review_response.schema.json",
    "merge_readiness": "merge_readiness.schema.json",
    "service_manifest": "service_manifest.schema.json",
    "test_environment": "test_environment.schema.json",
}


def _guess_schema(path: Path) -> str | None:
    stem = path.stem.lower()
    for key, schema in ARTIFACT_TO_SCHEMA.items():
        if stem.startswith(key):
            return schema
    return None


class Validator:
    def __init__(self, schema_dir: Path):
        self.schema_dir = schema_dir
        self.errors: list[str] = []
        self._cache: dict[str, dict] = {}

    def load(self, name: str) -> dict:
        if name not in self._cache:
            self._cache[name] = json.loads((self.schema_dir / name).read_text())
        return self._cache[name]

    def check(self, value, schema: dict, path: str, root: dict) -> None:
        if "$ref" in schema:
            ref = schema["$ref"]
            if ref.startswith("#/$defs/"):
                schema = root["$defs"][ref.split("/")[-1]]
            else:
                root = self.load(ref)
                schema = root
        types = schema.get("type")
        if types is not None:
            allowed = types if isinstance(types, list) else [types]
            if not any(self._is_type(value, t) for t in allowed):
                self.errors.append(f"{path}: expected {allowed}, got {type(value).__name__}")
                return
        if "enum" in schema and value not in schema["enum"]:
            self.errors.append(f"{path}: {value!r} not in {schema['enum']}")
        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                self.errors.append(f"{path}: {value!r} does not match /{schema['pattern']}/")
            if "minLength" in schema and len(value) < schema["minLength"]:
                self.errors.append(f"{path}: shorter than {schema['minLength']} chars")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                self.errors.append(f"{path}: longer than {schema['maxLength']} chars")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                self.errors.append(f"{path}: {value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                self.errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if isinstance(value, dict):
            for req in schema.get("required", []):
                if req not in value:
                    self.errors.append(f"{path}: missing required key '{req}'")
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for k in value:
                    if k not in props:
                        self.errors.append(f"{path}: unexpected key '{k}'")
            for k, v in value.items():
                if k in props:
                    self.check(v, props[k], f"{path}.{k}", root)
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                self.errors.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                self.errors.append(f"{path}: more than {schema['maxItems']} items")
            if "items" in schema:
                for i, item in enumerate(value):
                    self.check(item, schema["items"], f"{path}[{i}]", root)

    @staticmethod
    def _is_type(value, t: str) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }.get(t, False)


def validate_file(artifact: Path, schema_dir: Path, schema_name: str | None = None) -> list[str]:
    schema_name = schema_name or _guess_schema(artifact)
    if schema_name is None:
        return [f"cannot infer schema from filename '{artifact.name}'; pass --schema"]
    v = Validator(schema_dir)
    root = v.load(schema_name)
    data = json.loads(artifact.read_text())
    v.check(data, root, "$", root)
    return v.errors


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    artifact = Path(argv[1])
    schema_dir = Path("/mnt/session/uploads/schemas")
    schema_name = None
    args = argv[2:]
    while args:
        flag = args.pop(0)
        if flag == "--schema-dir" and args:
            schema_dir = Path(args.pop(0))
        elif flag == "--schema" and args:
            schema_name = args.pop(0)
        else:
            print(f"unknown argument {flag}")
            return 2
    if not artifact.exists():
        print(f"{artifact} does not exist")
        return 2
    errors = validate_file(artifact, schema_dir, schema_name)
    if errors:
        print(f"{artifact.name}: {len(errors)} schema error(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"{artifact.name}: valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
