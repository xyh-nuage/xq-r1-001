# t05 — L1 Spreadsheet Source Structure Validation

This target validates `L1-SPREADSHEET-SOURCE-STRUCTURE-001` deterministically.

- Source: `xyh-nuage/knowledge@66f33db182b98948e730db9d2daeeed041f36a6b`
- Frozen base: `23c63a6b0e50cd2fbbd530b3a3c6afa02a556e29`
- Real fixture source: `4d514701e01308129a76c609d23fe51a0ec92249`
- L0 changes are forbidden.
- LLM/model calls are forbidden.
- Complete workbook sheet/column schema is validated while row samples remain bounded.
- Hidden/helper sheet structure is preserved but hidden-sheet columns do not expand business-facing semantic column interpretation.
- Passing CI does not authorize merge or alter Research/Company/Shared reviewer gates.
