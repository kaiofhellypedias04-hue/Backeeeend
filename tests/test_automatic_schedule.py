from datetime import date, datetime
from unittest.mock import patch

import api


def test_agendar_execucao_delega_espera_ao_scheduler():
    req = api.ExecRequest(
        cert_aliases=["cert-a"],
        start=date(2026, 4, 1),
        end=date(2026, 4, 2),
        hora_execucao="06:00",
        lookback_days=1,
    )
    first_run_at = datetime(2026, 4, 17, 6, 0, 0)

    with patch("api._get_aliases_validos", return_value={"cert-a"}), patch(
        "api._calcular_proxima_execucao_horario",
        return_value=first_run_at,
    ), patch("api.iniciar_agendamento") as iniciar_mock:
        response = api.agendar_execucao(req)

    iniciar_mock.assert_called_once()
    kwargs = iniciar_mock.call_args.kwargs
    assert kwargs["first_run_at"] == first_run_at
    assert kwargs["func"].__name__ == "_executar_agendado_sem_espera"
    assert response["proxima_execucao"] == first_run_at.isoformat()
