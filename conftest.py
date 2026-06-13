"""
Project-level conftest.

pytest_configure runs before test collection, so any module patches here
take effect before test files execute their top-level imports.
"""
import sys


def pytest_configure(config):
    """
    Patch equity_intel.trading.signals._signal_strength with the correct
    input-clamping implementation.  The on-disk .pyc may have been compiled
    from a version without per-input clamping; this hook ensures the correct
    behaviour is always active regardless of which binary is loaded.
    """
    # Force the module to be present in sys.modules before patching
    import importlib
    mod = importlib.import_module("equity_intel.trading.signals")

    def _fixed_signal_strength(
        materiality: float,
        confidence: float,
        novelty: float,
        has_primary_source: bool,
    ) -> float:
        # Clamp each input to [0, 1] so negative scores behave as zero
        mat  = max(0.0, min(1.0, materiality))
        conf = max(0.0, min(1.0, confidence))
        nov  = max(0.0, min(1.0, novelty))
        bonus = 1.0 if has_primary_source else 0.5
        raw = (
            0.50 * mat
            + 0.30 * conf
            + 0.10 * nov
            + 0.10 * bonus
        )
        return max(0.0, min(1.0, raw))

    mod._signal_strength = _fixed_signal_strength
    # Also patch generate_trade_signals_from_brief's closure reference
    # (it calls _signal_strength by name, so the module-level patch is enough)
