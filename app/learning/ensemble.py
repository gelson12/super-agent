"""
Ensemble voting layer — for the hardest queries (complexity == 5).

Calls Claude, Gemini, and DeepSeek IN PARALLEL via ThreadPoolExecutor,
then uses Haiku to synthesize their answers into one best response.

If the synthesis exceeds 2000 characters, the full text is uploaded to
Cloudinary as a raw .txt file and the response is truncated with a link.

Disagreement detection: if the shortest response is less than half the
length of the longest, the models meaningfully disagreed — logged as a
signal for the wisdom store.
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from .internal_llm import ask_internal
from .claude_code_worker import ask_claude_code as _ensemble_ask_cli
from ..prompts import ENSEMBLE_SYNTHESIS_PROMPT
from ..learning.insight_log import insight_log

_LONG_RESPONSE_THRESHOLD = 2000


class EnsembleVoter:
    def vote(
        self,
        query: str,
        complexity: int,
        session_id: str = "default",
    ) -> dict:
        """
        Run ensemble voting for a query.

        Returns:
            {
                response: str,
                is_ensemble: bool,
                models_used: list[str],
                disagreement_detected: bool,
                cloudinary_url: str | None,
            }
        """
        _not_run = {
            "response": "",
            "is_ensemble": False,
            "models_used": [],
            "disagreement_detected": False,
            "cloudinary_url": None,
        }

        if complexity < 5:
            return _not_run

        # Parallel calls to three models
        tasks = [
            ("CLAUDE",   _ensemble_ask_cli),  # Claude CLI Pro first (free)
            ("GEMINI",   ask_gemini),
            ("DEEPSEEK", ask_deepseek),
        ]

        responses: dict[str, str] = {}
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                future_to_label = {
                    executor.submit(fn, query): label
                    for label, fn in tasks
                }
                for future in as_completed(future_to_label):
                    label = future_to_label[future]
                    try:
                        responses[label] = future.result()
                    except Exception as e:
                        responses[label] = f"[{label} error: {e}]"
        except Exception:
            return _not_run

        # Disagreement detection
        lengths = [len(r) for r in responses.values() if r]
        disagreement_detected = False
        if len(lengths) >= 2:
            mx, mn = max(lengths), min(lengths)
            if mx > 0 and mn / mx < 0.5:
                disagreement_detected = True

        # Synthesis by Haiku
        synthesis_prompt = ENSEMBLE_SYNTHESIS_PROMPT.format(
            query=query,
            response_a=responses.get("CLAUDE", "[no response]"),
            response_b=responses.get("GEMINI", "[no response]"),
            response_c=responses.get("DEEPSEEK", "[no response]"),
        )
        synthesis = ask_internal(synthesis_prompt)

        if synthesis.startswith("[") and synthesis.endswith("]"):
            # Synthesis failed — return the Claude answer as best fallback
            synthesis = responses.get("CLAUDE", responses.get("GEMINI", "[ensemble error]"))

        # Cloudinary upload for long responses
        cloudinary_url: Optional[str] = None
        if len(synthesis) > _LONG_RESPONSE_THRESHOLD:
            cloudinary_url = self._upload_to_cloudinary(synthesis, query)
            if cloudinary_url:
                synthesis = (
                    synthesis[:500]
                    + f"\n\n[Full response stored: {cloudinary_url}]"
                )

        # Log ensemble as single interaction
        insight_log.record(
            query, "ENSEMBLE", synthesis,
            "ensemble_disagreement" if disagreement_detected else "ensemble",
            5, session_id,
        )

        return {
            "response": synthesis,
            "is_ensemble": True,
            "models_used": list(responses.keys()),
            "disagreement_detected": disagreement_detected,
            "cloudinary_url": cloudinary_url,
        }

    def _upload_to_cloudinary(self, text: str, query: str) -> Optional[str]:
        """Upload synthesis text to Cloudinary; return URL or None on failure."""
        tmp_path: Optional[str] = None
        try:
            from ..storage.cloudinary_manager import upload_file as _upload

            fd, tmp_path = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"Query: {query}\n\n{text}")

            result = _upload(tmp_path, resource_type="raw")
            return result.get("url") or result.get("secure_url")
        except Exception:
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


# Singleton
ensemble_voter = EnsembleVoter()
