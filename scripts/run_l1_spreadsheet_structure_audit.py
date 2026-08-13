#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _find_one(files: list[Path], needle: str) -> Path:
    matches = [path for path in files if needle in path.name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one fixture for audit selector {needle!r}; got {len(matches)}")
    return matches[0]


def _primary_table(structure: dict) -> dict:
    tables = [
        table
        for sheet in structure.get("sheets", [])
        if sheet.get("visible")
        for table in sheet.get("tables", [])
    ]
    if not tables:
        raise AssertionError("expected at least one visible table")
    return max(
        tables,
        key=lambda table: (
            len(table.get("source_columns") or []),
            int(table.get("end_row", 0)) - int(table.get("start_row", 0)),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation-root", required=True)
    parser.add_argument("--fixture-root", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--fixture-sha", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    implementation_root = Path(args.implementation_root).resolve()
    fixture_root = Path(args.fixture_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(implementation_root / "l1"))

    from l1_processor.experiments.factorized_artifact_spreadsheet_source import (
        read_spreadsheet_source_structure,
    )

    files = sorted(
        path
        for path in fixture_root.iterdir()
        if path.is_file() and path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}
    )
    assert len(files) == 92, f"fixture count changed: {len(files)}"

    aggregate = Counter()
    private_details: list[dict] = []
    for path in files:
        try:
            structure = read_spreadsheet_source_structure(path)
        except Exception as exc:
            aggregate["read_failures"] += 1
            private_details.append(
                {
                    "file": path.name,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
            continue

        assert structure["schema_complete"] is True
        assert structure["row_values_complete"] is False
        assert structure["sample_rows_per_table"] == 2
        aggregate["read_success"] += 1
        aggregate["sheets"] += int(structure.get("sheet_count", 0))
        aggregate["visible_sheets"] += sum(1 for sheet in structure.get("sheets", []) if sheet.get("visible"))
        aggregate["hidden_sheets"] += sum(1 for sheet in structure.get("sheets", []) if not sheet.get("visible"))
        tables = [table for sheet in structure.get("sheets", []) for table in sheet.get("tables", [])]
        aggregate["tables"] += len(tables)
        aggregate["unique_source_columns"] += len(structure.get("source_columns", []))
        for sheet in structure.get("sheets", []):
            for table in sheet.get("tables", []):
                samples = list(table.get("sample_rows") or [])
                assert len(samples) <= 2
                if not sheet.get("visible"):
                    assert samples == []
                aggregate["sample_rows"] += len(samples)
        private_details.append(
            {
                "file": path.name,
                "status": "succeeded",
                "sheet_count": structure.get("sheet_count"),
                "visible_sheet_count": sum(1 for sheet in structure.get("sheets", []) if sheet.get("visible")),
                "hidden_sheet_count": sum(1 for sheet in structure.get("sheets", []) if not sheet.get("visible")),
                "table_count": len(tables),
                "source_column_count": len(structure.get("source_columns", [])),
                "structure_sha256": _sha256_json(structure),
            }
        )

    assert aggregate["read_failures"] == 0
    assert aggregate["read_success"] == 92

    inventory = _find_one(files, "新创云联产业发展有限公司2026年6月30日.xls")
    inventory_structure = read_spreadsheet_source_structure(inventory)
    inventory_table = _primary_table(inventory_structure)
    inventory_columns = list(inventory_table.get("source_columns") or [])
    assert len(inventory_columns) == 37
    assert inventory_columns[-1] == "捆号"
    for expected in ("已出件数", "已出量(kg)", "货物备注", "在库(天)", "结算单位", "捆号"):
        assert expected in inventory_columns

    logistics = _find_one(files, "20260629_杰坤-小田_新创云联 物流跟踪表.xlsx")
    logistics_structure = read_spreadsheet_source_structure(logistics)
    logistics_table = _primary_table(logistics_structure)
    logistics_columns = list(logistics_table.get("source_columns") or [])
    assert len(logistics_columns) == 26
    for expected in (
        "流向",
        "免箱期",
        "免箱期\n截止",
        "免堆期\n截止\n基础7天",
        "缺单据情况",
        "免箱期非21天请说明原因",
    ):
        assert expected in logistics_columns

    lineup = _find_one(files, "20260624_金德周贤俊13409031539_PORT LINE-UP 2026-6-24.xlsx")
    lineup_structure = read_spreadsheet_source_structure(lineup)
    lineup_sheets = {sheet["sheet"]: sheet for sheet in lineup_structure.get("sheets", [])}
    assert lineup_structure["sheet_count"] == 9
    assert sum(1 for sheet in lineup_sheets.values() if sheet["visible"]) == 7
    assert "LANQIAO" in lineup_sheets and lineup_sheets["LANQIAO"]["visible"] is True
    assert "QINGDAO" in lineup_sheets and lineup_sheets["QINGDAO"]["visible"] is True
    for sheet in lineup_sheets.values():
        if not sheet["visible"]:
            assert all(table.get("sample_rows") == [] for table in sheet.get("tables", []))

    summary = {
        "schema_version": "l1_spreadsheet_structure_audit.v1",
        "source_sha": args.source_sha,
        "fixture_sha": args.fixture_sha,
        "files_total": len(files),
        "read_success": aggregate["read_success"],
        "read_failures": aggregate["read_failures"],
        "sheets_total": aggregate["sheets"],
        "visible_sheets_total": aggregate["visible_sheets"],
        "hidden_sheets_total": aggregate["hidden_sheets"],
        "tables_total": aggregate["tables"],
        "sample_rows_total": aggregate["sample_rows"],
        "sample_rows_per_table_max": 2,
        "row_values_complete": False,
        "inventory_37_columns": True,
        "logistics_26_columns": True,
        "lineup_all_9_sheets": True,
        "lineup_all_7_visible_sheets": True,
        "hidden_sheet_samples_empty": True,
        "no_llm": True,
        "l0_changes_expected": False,
    }
    details = {
        "schema_version": "l1_spreadsheet_structure_audit_details.v1",
        "source_sha": args.source_sha,
        "fixture_sha": args.fixture_sha,
        "files": private_details,
        "targeted_checks": {
            "inventory_column_count": len(inventory_columns),
            "inventory_columns_sha256": _sha256_json(inventory_columns),
            "logistics_column_count": len(logistics_columns),
            "logistics_columns_sha256": _sha256_json(logistics_columns),
            "lineup_sheet_count": lineup_structure["sheet_count"],
            "lineup_visible_sheet_count": sum(1 for sheet in lineup_sheets.values() if sheet["visible"]),
            "lineup_sheet_names_sha256": _sha256_json(list(lineup_sheets)),
        },
        "no_cell_values_persisted": True,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    for key in (
        "files_total",
        "read_success",
        "read_failures",
        "sheets_total",
        "visible_sheets_total",
        "hidden_sheets_total",
        "tables_total",
        "sample_rows_total",
    ):
        print(f"{key}={summary[key]}")
    print("inventory_37_columns=PASS")
    print("logistics_26_columns=PASS")
    print("lineup_all_visible_sheets=PASS")
    print("L1_SPREADSHEET_STRUCTURE_AUDIT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
