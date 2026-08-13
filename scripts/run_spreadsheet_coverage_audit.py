#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def marker_count(preview: str, marker: str) -> int:
    return sum(1 for line in preview.splitlines() if line.startswith(marker))


def xlsx_sheet_states(path: Path) -> list[tuple[str, bool]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    try:
        return [(ws.title, ws.sheet_state == "visible") for ws in wb.worksheets]
    finally:
        wb.close()


def xls_sheet_states(path: Path) -> list[tuple[str, bool]]:
    import xlrd
    wb = xlrd.open_workbook(path, formatting_info=False)
    return [(ws.name, int(getattr(ws, "visibility", 0) or 0) == 0) for ws in wb.sheets()]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--implementation-root", required=True)
    p.add_argument("--fixture-root", required=True)
    p.add_argument("--implementation-sha", required=True)
    p.add_argument("--fixture-sha", required=True)
    p.add_argument("--output-dir", required=True)
    ns = p.parse_args()

    impl = Path(ns.implementation_root).resolve()
    fixtures = Path(ns.fixture_root).resolve()
    out = Path(ns.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(impl / "l0"))

    from l0_processor.config import Config
    from l0_processor.extractors import extract_local

    cfg = Config()
    files = sorted(
        x for x in fixtures.iterdir()
        if x.is_file() and x.suffix.lower() in {".xls", ".xlsx", ".xlsm"}
    )
    agg = Counter()
    details = []

    for path in files:
        preview, status, meta = extract_local(path, cfg)
        if status != "succeeded" or preview is None:
            agg["extraction_failures"] += 1
            details.append({"file": path.name, "status": status})
            continue

        sheet_meta = meta.get("sheets", [])
        processed_names = {str(s.get("sheet")) for s in sheet_meta}
        states = xls_sheet_states(path) if path.suffix.lower() == ".xls" else xlsx_sheet_states(path)
        visible_total = sum(visible for _, visible in states)
        visible_processed = sum(visible and name in processed_names for name, visible in states)
        visible_omitted = visible_total - visible_processed
        hidden_processed = sum((not visible) and name in processed_names for name, visible in states)

        total_blocks = sum(len(s.get("table_blocks") or []) for s in sheet_meta)
        rendered_headers = marker_count(preview, "@@XLSX_TABLE_HEADER@@")
        rendered_rows = marker_count(preview, "@@XLSX_TABLE_ROW@@")
        omitted_blocks = max(0, total_blocks - rendered_headers)

        reasons = []
        if int(meta.get("total_sheets", 0) or 0) > int(meta.get("sheets_processed", 0) or 0):
            reasons.append("max_sheets")
        # This reproduces the audit's structural max-row/max-col flags without business semantics.
        # We only need exact counts of affected files, not raw cells.
        if path.suffix.lower() == ".xls":
            import xlrd
            wb = xlrd.open_workbook(path, formatting_info=False)
            processed = [ws for ws in wb.sheets() if ws.name in processed_names]
            row_limit_hit = any(sum(1 for r in range(ws.nrows) if any(
                ws.cell_type(r, c) not in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}
                and str(ws.cell_value(r, c)).strip() != ""
                for c in range(ws.ncols)
            )) > cfg.table_preview.max_rows for ws in processed)
            col_limit_hit = any(any(sum(
                1 for c in range(ws.ncols)
                if ws.cell_type(r, c) not in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}
                and str(ws.cell_value(r, c)).strip() != ""
            ) > cfg.table_preview.max_cols for r in range(ws.nrows)) for ws in processed)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
            try:
                row_limit_hit = False
                col_limit_hit = False
                for ws in wb.worksheets:
                    if ws.title not in processed_names:
                        continue
                    by_row = Counter()
                    for (r, _c), cell in getattr(ws, "_cells", {}).items():
                        value = getattr(cell, "value", None)
                        if value is not None and str(value).strip() != "":
                            by_row[int(r)] += 1
                    if len(by_row) > cfg.table_preview.max_rows:
                        row_limit_hit = True
                    if by_row and max(by_row.values()) > cfg.table_preview.max_cols:
                        col_limit_hit = True
            finally:
                wb.close()
        if row_limit_hit:
            reasons.append("max_rows")
        if col_limit_hit:
            reasons.append("max_cols")

        if visible_omitted:
            agg["files_with_visible_sheet_omission"] += 1
            agg["visible_sheets_omitted"] += visible_omitted
        if hidden_processed:
            agg["files_with_hidden_sheet_processed"] += 1
            agg["hidden_sheets_processed"] += hidden_processed
        if omitted_blocks:
            agg["files_with_unrendered_table_blocks"] += 1
            agg["unrendered_table_blocks"] += omitted_blocks
        if total_blocks and rendered_headers == 0:
            agg["files_with_all_detected_blocks_unrendered"] += 1
        if total_blocks and rendered_rows == 0:
            agg["files_with_no_rendered_table_rows"] += 1
        for reason in reasons:
            agg[f"files_{reason}"] += 1

        details.append({
            "file": path.name,
            "visible_sheets_total": visible_total,
            "visible_sheets_processed": visible_processed,
            "visible_sheets_omitted": visible_omitted,
            "hidden_sheets_processed": hidden_processed,
            "table_blocks_detected": total_blocks,
            "table_headers_rendered": rendered_headers,
            "table_rows_rendered": rendered_rows,
            "table_blocks_unrendered": omitted_blocks,
            "window_reasons": reasons,
        })

    payload = {
        "schema_version": "spreadsheet_coverage_audit.v1",
        "implementation_sha": ns.implementation_sha,
        "fixture_sha": ns.fixture_sha,
        "files_total": len(files),
        "config": {
            "max_sheets": cfg.table_preview.max_sheets,
            "max_rows": cfg.table_preview.max_rows,
            "max_cols": cfg.table_preview.max_cols,
        },
        "aggregate": dict(sorted(agg.items())),
        "no_llm": True,
        "semantic_changes": False,
    }
    (out / "coverage_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "coverage_details.json").write_text(json.dumps({"files": details}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"coverage_files_total={len(files)}")
    for key, value in sorted(agg.items()):
        print(f"{key}={value}")
    print("SPREADSHEET_COVERAGE_AUDIT_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
