# TODO: Correção Classificação Notas (Incluir status_base_calculo na regra final com normalização OK values)

**Plano aprovado:**
1. ✅ Create TODO.md 
2. ✅ Edit `modules/fiscal_status.py`: Added `"status_base_calculo"` to `FINAL_STATUS_FIELDS`; updated comment.
3. ✅ Edit `tests/test_fiscal_status.py`: Revised test to expect `"divergente"` when `status_base_calculo="divergente"`.
4. ✅ Verify `notas_repo.py` STATUS_EXPR updates automatically (no edit needed).
5. ✅ Tests updated and logic correct (validation manual due to Windows shell; STATUS_EXPR now includes status_base_calculo).
6. Show final diffs.
7. attempt_completion.

