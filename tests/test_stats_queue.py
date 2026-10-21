"""``refresh_stats_snapshot`` punya antrean sendiri (21 Oktober 2026).

Kasus 0704449OzZ: penghitung ulang Statistics terjadwal tiap 2 menit, tetapi
antre di belakang 8 evaluasi LLM (batch JOB-3 15.00 WIB) di antrean bawaan — TL
QC meng-approve 15.01.13, snapshot baru menyusul 15.03.39. Task itu kini dirutekan
ke antrean ``stats`` yang dikerjakan service ``worker-stats`` (concurrency 1);
worker evaluasi tidak mengambil antrean itu.
"""
import re
from pathlib import Path

from worker.celery_app import celery_app

TASK = "worker.tasks.maintenance.refresh_stats_snapshot"
COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_task_dirutekan_ke_antrean_stats():
    route = celery_app.amqp.router.route({}, TASK, args=(), kwargs={})
    assert route["queue"].name == "stats"


def test_jadwal_beat_kedaluwarsa_sebelum_jadwal_berikutnya():
    entry = celery_app.conf.beat_schedule["refresh-stats-snapshot"]
    # Kalau worker-stats mati, pesan tidak menumpuk: yang lewat jadwal dibuang.
    assert 0 < entry["options"]["expires"] <= entry["schedule"]


def _service_command(text: str, name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}:\n(?:    .*\n|\s*\n)*?    command: (.+)$", text, re.M)
    assert m, f"service {name} / command-nya tidak ada di docker-compose.yml"
    return m.group(1)


def test_compose_punya_worker_stats_dan_worker_utama_tidak_mengambilnya():
    text = COMPOSE.read_text()
    cmd = _service_command(text, "worker-stats")
    assert "-Q stats" in cmd and "--concurrency=1" in cmd
    assert "stats" not in _service_command(text, "worker")
