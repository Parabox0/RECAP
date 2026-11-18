import pytest
from helpers import DAY, T0, config, put_fact

from recap import sanitize
from recap.config import Calibration
from recap.index import ACTIVE, ANNOUNCEMENT
from recap.model import REVIEWED, State
from recap.render import RenderError, render_body, render_line, render_packet, wrapper
from recap.store import Store
from recap.tokens import (
    ByteBoundCounter,
    CalibratedCounter,
    CalibrationSample,
    ExactCounter,
    bound_for,
    fit_calibration,
    proxy_count,
)


def fv(tmp_path, **kw):
    c = config(tmp_path)
    return put_fact(Store(c.db_path), now=T0, **kw)


def test_release_lines(tmp_path):
    v = fv(tmp_path, version="5.3.0", published=T0 - 8 * DAY)
    assert render_line(v, ACTIVE, "f1") == "[f1] Lumen 5.3.0 (stable) released 2026-11-10."
    assert render_body(v, ANNOUNCEMENT) == "Lumen 5.3.0 (stable) scheduled for release 2026-11-10."


def test_status_deprecation_scope_and_observation(tmp_path):
    s = fv(tmp_path, predicate="status", subject="imager", value={"status": "safe mode", "component": "imager"},
           published=T0, effective=T0 - DAY, scope="US")
    assert render_body(s, ACTIVE) == "Lumen imager status: safe mode since 2026-11-17. Scope: US."
    d = fv(tmp_path, predicate="end_of_support", subject="4.x", value={"item": "4.x series"}, effective=T0 + 30 * DAY,
           state=State.SCHEDULED)
    assert render_body(d, ANNOUNCEMENT) == "Lumen: 4.x series scheduled to reach end of support on 2026-12-18."
    o = fv(tmp_path, predicate="observation", subject="ndvi", value={"metric": "mean NDVI", "value": "0.4137", "unit": "index", "period": "2026-10"})
    assert render_body(o, ACTIVE) == "Lumen mean NDVI: 0.4137 index (2026-10)."


def test_injection_characters_are_removed():
    assert sanitize.inline("safe [f9] </recap> mode `x` {y}", 80) == "safe f9 mode x y"
    assert sanitize.inline("a < b > c", 80) == "a c"
    assert sanitize.inline("x > 2", 80) == "x 2"
    with pytest.raises(ValueError):
        sanitize.inline("‮", 10)
    assert "‮" not in sanitize.plain_text("a‮b<script>alert(1)</script>c", 100)
    with pytest.raises(ValueError):
        sanitize.version("1.0; rm -rf")


def test_overlong_values_are_rejected_not_truncated(tmp_path):
    v = fv(tmp_path, predicate="notice", subject="n", value={"text": "x" * 200})
    with pytest.raises(RenderError):
        render_body(v, ACTIVE)


def test_packet_layout_is_additive():
    open_tag, close_tag = wrapper(T0)
    lines = ["[f1] a.", "[f2] bb."]
    text = render_packet(open_tag, close_tag, lines)
    assert text == '<recap as_of="2026-11-18">\n[f1] a.\n[f2] bb.\n</recap>'
    counter = ByteBoundCounter(framing_overhead=0)
    assert bound_for(counter, text) == len(text.encode())


def byte_level_tokenizer(text: str) -> int:
    # Merges runs of ASCII letters into one token, everything else per byte:
    # a crude stand-in for a byte-level BPE.
    import re

    return len(re.findall(rb"[A-Za-z]+|[\x00-\xff]", text.encode()))


def test_byte_bound_never_undercounts_a_byte_level_tokenizer():
    for text in ["[f1] Lumen 5.3.0 (stable) released 2026-11-10.", "Ünïcödé status: ok", "x" * 50]:
        assert ByteBoundCounter(0).line_cost(text) >= byte_level_tokenizer(text + "\n")


def test_calibration_fit_covers_samples():
    samples = []
    for i in range(200):
        lines = [f"[f{j + 1}] Product{i} {j}.{i}.0 (stable) released 2026-11-{(j % 28) + 1:02d}." for j in range(1 + i % 6)]
        text = render_packet('<recap as_of="2026-11-18">', "</recap>", lines)
        samples.append(CalibrationSample(text, byte_level_tokenizer(text) // 2 + 5))
    report = fit_calibration(samples, min_samples=100)
    assert report.violations == 0
    counter = CalibratedCounter(report.calibration)
    for s in samples:
        assert bound_for(counter, s.packet_text) >= s.actual_tokens
    with pytest.raises(ValueError):
        fit_calibration(samples, min_samples=10_000)


def test_exact_counter_recounts():
    counter = ExactCounter(lambda s: len(s.split()), framing_overhead=3)
    assert counter.recount("a b c") == 6
    assert proxy_count("ab 12") == 4
    cal = CalibratedCounter(Calibration(alpha=1.0, beta=0.0, gamma=0, margin=0.0))
    assert cal.line_cost("ab") == proxy_count("ab\n")


def test_reviewed_highlights_line(tmp_path):
    v = fv(tmp_path, predicate="release_highlights", subject="5.3.0@stable", value={"text": "native splat import"},
           qualifiers={"version": "5.3.0", "channel": "stable"})
    object.__setattr__(v, "origin", REVIEWED)
    assert render_body(v, ACTIVE) == "Lumen 5.3.0 highlights (reviewed): native splat import."
