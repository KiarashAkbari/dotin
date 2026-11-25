"""The whole chain on tiny synthetic data, from an empty database to a prediction (brief section 7).

One command -- ``scripts/run_chain.py`` -- walks ingest (stage 1's loader), the audit, the SQL
feature layer, the model and the 10 % list against a *throwaway* PostgreSQL database; then
``capstone predict`` scores one carved-out month through the same views and must agree with the
model step's own scores, and a broken bundle must be refused with exit 65.

Destructive, so it runs only when the reader says so, with the same switch as the smoke tests::

    CAPSTONE_SMOKE_DATABASE_URI=postgresql://user:pw@localhost:5432/throwaway \\
    CAPSTONE_SMOKE_ALLOW_DESTROY=1 python -m unittest test_end_to_end

It drops and recreates the ``olist`` and ``capstone`` schemas of that database. The external source
is declared ``none`` here, so the test needs no network.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
import synthetic_olist

STAGE = helpers.STAGE_ROOT
URI = os.environ.get("CAPSTONE_SMOKE_DATABASE_URI", "").strip()
ALLOWED = os.environ.get("CAPSTONE_SMOKE_ALLOW_DESTROY", "") == "1"
CONFIG_NAME = "_e2e_test.toml"  # inside config/ because every path in a config is relative to it
CURATED = "../data/curated/_e2e"


def e2e_config() -> str:
    text = helpers.DEFAULT_CONFIG.read_text(encoding="utf-8")
    text = text.replace('curated_dir = "../data/curated"', f'curated_dir = "{CURATED}"')
    text = text.replace(
        'families = ["logreg.c0_1", "logreg.c1", "hgb.d3", "hgb.d6"]',
        'families = ["logreg.c1", "hgb.d3"]',
    )
    start = text.index("[external]")
    end = text.index("[ingest]")
    text = text[:start] + '[external]\nname = "none"\nurl = ""\ntoken_env = ""\njoin_on = ""\n\n' + text[end:]
    return text


@unittest.skipUnless(URI and ALLOWED, "needs CAPSTONE_SMOKE_DATABASE_URI and CAPSTONE_SMOKE_ALLOW_DESTROY=1")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        with psycopg.connect(URI, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS capstone CASCADE")
            connection.execute("DROP SCHEMA IF EXISTS olist CASCADE")
            connection.execute("DROP SCHEMA IF EXISTS capstone_predict_stage CASCADE")
        cls.tmp = Path(tempfile.mkdtemp(prefix="capstone-e2e-"))
        cls.olist = cls.tmp / "olist"
        cls.months = synthetic_olist.write(cls.olist)
        cls.config = STAGE / "config" / CONFIG_NAME
        cls.config.write_text(e2e_config(), encoding="utf-8")
        cls.curated = (STAGE / "config" / CURATED).resolve()
        shutil.rmtree(cls.curated, ignore_errors=True)
        cls.env = dict(
            os.environ,
            CAPSTONE_DATABASE_URI=URI,
            CAPSTONE_OLIST_DIR=str(cls.olist),
            PYTHONPATH=os.pathsep.join(
                [str(STAGE / "src"), str(STAGE.parent / "stage-1-kiarash-akbari" / "src")]
            ),
        )
        cls.manifest = cls.tmp / "manifest.json"
        cls.chain = subprocess.run(
            [sys.executable, str(STAGE / "scripts" / "run_chain.py"), "--config", str(cls.config),
             "--manifest", str(cls.manifest)],
            cwd=STAGE, env=cls.env, capture_output=True, text=True, check=False, timeout=900,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.config.unlink(missing_ok=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.curated, ignore_errors=True)

    def predict(self, bundle: Path, out: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "capstone", "predict", "--config", str(self.config),
             "--input", str(bundle), "--out", str(out)],
            cwd=STAGE, env=self.env, capture_output=True, text=True, check=False, timeout=300,
        )

    def test_1_one_command_runs_every_step_and_times_it(self) -> None:
        self.assertEqual(self.chain.returncode, 0, self.chain.stderr[-2000:])
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            [layer["name"] for layer in manifest["layers"]],
            ["ingest", "load-database", "load-audit", "features", "external", "model", "capacity-list"],
        )
        self.assertEqual(manifest["total"]["status"], "ok")

    def test_2_the_list_is_the_top_tenth_of_the_evaluation_window(self) -> None:
        self.assertEqual(self.chain.returncode, 0, self.chain.stderr[-2000:])
        with (self.curated / "followup_list.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        table = json.loads((self.curated / "capacity_table.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), table["list_rows"])
        self.assertEqual(table["list_rows"], -(-table["window_rows"] // 10))
        scores = [float(row["score"]) for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        approved = {row["order_approved_at"][:10] for row in rows}
        self.assertTrue(all(day > "2018-05-31" for day in approved), "the list is evaluation orders only")

    def test_3_predict_on_a_carved_month_reproduces_the_model_step(self) -> None:
        self.assertEqual(self.chain.returncode, 0, self.chain.stderr[-2000:])
        sys.path.insert(0, str(STAGE / "scripts"))
        import sealed_cut_rehearsal

        bundle = self.tmp / "cut"
        ids = sealed_cut_rehearsal.carve(self.olist, "2018-07", bundle)
        out = self.tmp / "ranked.csv"
        result = self.predict(bundle, out)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        with out.open(encoding="utf-8") as handle:
            ranked = {row["order_id"]: float(row["score"]) for row in csv.DictReader(handle)}
        self.assertEqual(set(ranked), set(ids) - self._unapproved(bundle))
        import psycopg

        with psycopg.connect(URI) as connection:
            stored = dict(
                connection.execute(
                    "SELECT order_id, score FROM capstone.order_score WHERE order_id = ANY(%s) AND "
                    "run_id = (SELECT run_id FROM capstone.model_run ORDER BY created_at DESC LIMIT 1)",
                    (list(ranked),),
                ).fetchall()
            )
            orders = connection.execute("SELECT count(*) FROM olist.orders").fetchone()[0]
        self.assertEqual(set(stored), set(ranked))
        self.assertLess(max(abs(stored[k] - ranked[k]) for k in ranked), 1e-5)
        self.assertEqual(orders, 900, "the prediction transaction is rolled back")

    def test_4_a_bundle_with_a_wrong_column_is_refused_with_exit_65(self) -> None:
        self.assertEqual(self.chain.returncode, 0, self.chain.stderr[-2000:])
        bundle = self.tmp / "bad"
        shutil.copytree(self.olist, bundle)
        path = bundle / "olist_order_payments_dataset.csv"
        text = path.read_text(encoding="utf-8").replace("payment_value", "amount", 1)
        path.write_text(text, encoding="utf-8")
        result = self.predict(bundle, self.tmp / "never.csv")
        self.assertEqual(result.returncode, 65, result.stderr)
        self.assertIn("amount", result.stderr)
        self.assertFalse((self.tmp / "never.csv").exists())

    @staticmethod
    def _unapproved(bundle: Path) -> set[str]:
        with (bundle / "olist_orders_dataset.csv").open(encoding="utf-8") as handle:
            return {row["order_id"] for row in csv.DictReader(handle) if not row["order_approved_at"]}


if __name__ == "__main__":
    unittest.main()
