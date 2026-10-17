"""Compose contract for the dedicated Klyrow SES mail worker."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")


def compose_block(start_marker: str, end_marker: str | None = None) -> str:
    start = COMPOSE.index(start_marker)
    if end_marker is None:
        return COMPOSE[start:]
    end = COMPOSE.index(end_marker, start + len(start_marker))
    return COMPOSE[start:end]


class SesWorkerComposeContractTest(unittest.TestCase):
    def test_worker_explicitly_selects_transactional_ses(self) -> None:
        worker = compose_block("  worker:\n", "\n  scheduler:")

        self.assertIn("KLYROW_EMAIL_TRANSPORT: ses", worker)
        self.assertIn("KLYROW_SES_REGION: us-east-1", worker)
        self.assertIn(
            "KLYROW_SES_CONFIGURATION_SET: codestra-klyrow-transactional",
            worker,
        )
        self.assertIn('KLYROW_SES_CANARY_ONLY: "true"', worker)
        self.assertIn('KLYROW_SES_LIVE_APPROVED: "false"', worker)

    def test_worker_uses_file_backed_ses_smtp_credentials(self) -> None:
        worker = compose_block("  worker:\n", "\n  scheduler:")
        secrets = compose_block("secrets:\n")

        self.assertIn(
            "KLYROW_SES_SMTP_USERNAME_FILE: /run/secrets/klyrow_ses_smtp_username",
            worker,
        )
        self.assertIn(
            "KLYROW_SES_SMTP_PASSWORD_FILE: /run/secrets/klyrow_ses_smtp_password",
            worker,
        )
        self.assertIn("klyrow_ses_smtp_username", worker)
        self.assertIn("klyrow_ses_smtp_password", worker)
        self.assertIn("  klyrow_ses_smtp_username:\n", secrets)
        self.assertIn("  klyrow_ses_smtp_password:\n", secrets)

    def test_worker_keeps_external_delivery_gates_closed(self) -> None:
        worker = compose_block("  worker:\n", "\n  scheduler:")

        self.assertIn('KLYROW_SAFE_MODE: "true"', worker)
        self.assertIn('KLYROW_PRODUCTION_GATE_APPROVED: "false"', worker)
        self.assertIn('LIVE_EMAIL_DELIVERY: "false"', worker)
        self.assertIn('EXTERNAL_EMAIL_DELIVERY: "false"', worker)
        self.assertIn('PRODUCTION_PROVIDER_ROUTING: "false"', worker)

    def test_global_env_example_does_not_select_ses_for_gateway(self) -> None:
        self.assertNotIn("KLYROW_EMAIL_TRANSPORT=ses", ENV_EXAMPLE)
        self.assertNotIn("KLYROW_SES_REGION=us-east-1", ENV_EXAMPLE)
        self.assertNotIn(
            "KLYROW_SES_CONFIGURATION_SET=codestra-klyrow-transactional",
            ENV_EXAMPLE,
        )


if __name__ == "__main__":
    unittest.main()
