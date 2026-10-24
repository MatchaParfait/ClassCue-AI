"""Focused selection checks; no recordings, network, or model services."""

from pathlib import Path

import cv2
import numpy as np

from classcue.slide_extractor import (
    SlideRoi, _Candidate, _feature, _information_score, _preferred_candidates,
)


def page(*lines):
    image = np.full((360, 640, 3), 245, np.uint8)
    for index, line in enumerate(lines):
        cv2.putText(image, line, (40, 70 + index * 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2)
    return image


def select(images):
    candidates = []
    for index, image in enumerate(images):
        feature = _feature(image, SlideRoi(0, 0, 640, 360), (), 640)
        candidates.append(_Candidate(
            index * 5000, index * 5000, (index + 1) * 5000,
            index * 5000 + 4000, feature, Path(f"{index}.jpg"), str(index),
            _information_score(feature),
        ))
    return _preferred_candidates(candidates)


def test_identical_states_keep_one_representative():
    image = page("Question", "x = a + b")
    assert len(select([image.copy() for _ in range(6)])) == 1


def test_small_recording_noise_does_not_keep_every_state():
    base = page("Question", "x = a + b")
    images = []
    for seed in range(8):
        noise = np.random.default_rng(seed).integers(-4, 5, base.shape)
        images.append(np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8))
    assert len(select(images)) == 1


def test_faint_edges_toggling_with_compression_do_not_count_as_lost_content():
    images = []
    for color in (224, 222, 225, 223, 221):
        image = page("Question", "x = a + b")
        cv2.putText(image, "faint watermark", (250, 250), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (color, color, color), 1)
        images.append(image)
    assert len(select(images)) == 1


def test_changing_footer_subtitles_do_not_multiply_pages():
    base = page("Question", "x = a + b", "y = 2 * x")
    images = []
    for text in ("First explanation", "Another spoken sentence", "Now consider the method",
                 "Then simplify this expression", "Finally we have the answer"):
        image = base.copy()
        cv2.putText(image, text, (100, 335), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (20, 20, 20), 1)
        images.append(image)
    assert len(select(images)) == 1


def test_footer_filter_keeps_replaced_main_formula():
    images = []
    for index, text in enumerate(("First sentence", "Another explanation",
                                   "Consider this method", "Now change the operator",
                                   "Use the corrected expression", "Finally simplify")):
        image = page("Question", "x = a + b" if index < 3 else "x = a / b")
        cv2.putText(image, text, (100, 335), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (20, 20, 20), 1)
        images.append(image)
    indices = select(images)
    assert len(indices) == 2
    assert any(index < 3 for index in indices)
    assert any(index >= 3 for index in indices)


def test_incremental_bottom_formulas_are_not_a_subtitle_band():
    images = [page("Question")]
    for line in ("x = a", "y = 2 * x", "z = x + y", "Result = z"):
        image = images[-1].copy()
        x = 10 + 150 * (len(images) - 1)
        cv2.putText(image, line, (x, 335), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (20, 20, 20), 1)
        images.append(image)
    assert select(images) == {4}


def test_replaced_bottom_formula_is_preserved():
    first = page("Question")
    second = first.copy()
    cv2.putText(first, "x = a + b", (80, 335), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2)
    cv2.putText(second, "x = a / b", (80, 335), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2)
    assert select([first, second]) == {0, 1}


def test_complementary_question_and_answer_are_both_selected():
    assert select([page("Question: x + y = ?"), page("Answer: 42")]) == {0, 1}


def test_small_symbol_modification_is_not_duplicate():
    first = page("Question", "x = 2")
    cv2.line(first, (300, 160), (324, 160), (20, 20, 20), 2)
    second = first.copy()
    cv2.line(second, (312, 148), (312, 172), (20, 20, 20), 2)
    assert select([first, second]) == {0, 1}


def test_changed_existing_text_color_is_not_duplicate():
    first = page("Question")
    second = np.full_like(first, 245)
    cv2.putText(second, "Question", (40, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 20, 20), 2)
    assert select([first, second]) == {0, 1}


def test_removed_derivation_is_represented_by_more_complete_state():
    assert select([page("x = a + b", "y = 2 * x"), page("x = a + b")]) == {0}
