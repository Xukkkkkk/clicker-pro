"""Deterministic precision regressions; all captures and clicks are synthetic."""

import cv2
import numpy as np
import pytest
from unittest.mock import Mock

from vision_engine import TemplateSpec, VisionEngine


def scan_image(template, frame, threshold=.95):
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=threshold)],
        capture_fn=lambda: frame,
    )
    matches = engine.scan_once(trigger=False)
    assert engine.last_error is None
    return matches


def test_exact_black_template_is_detected():
    # SQDIFF_NORMED returns 1 even for a perfect black-on-black match:
    # its normalization denominator is zero.
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    frame = np.full((100, 140, 3), 220, dtype=np.uint8)
    frame[40:60, 60:90] = template

    matches = scan_image(template, frame, threshold=.99)

    assert len(matches) == 1
    assert matches[0].rect == (60, 40, 30, 20)
    assert matches[0].score >= .99


@pytest.mark.parametrize("low_texture", [False, True])
def test_large_color_change_cannot_score_as_an_exact_match(low_texture):
    if low_texture:
        template = np.full((32, 48, 3), 80, dtype=np.uint8)
        template[10:20, 10:30] = 85
    else:
        template = np.random.default_rng(28).integers(
            20, 100, (32, 48, 3), dtype=np.uint8,
        )
    frame = np.full((100, 160, 3), 220, dtype=np.uint8)
    frame[30:62, 80:128] = template + 120

    assert scan_image(template, frame) == []


def test_color_decoy_does_not_hide_slightly_noisy_correct_target():
    rng = np.random.default_rng(28)
    template = rng.integers(20, 100, (32, 48, 3), dtype=np.uint8)
    noise = rng.integers(-1, 2, template.shape, dtype=np.int16)
    target = (template.astype(np.int16) + noise).astype(np.uint8)
    frame = np.full((140, 200, 3), 220, dtype=np.uint8)
    frame[10:42, 10:58] = template + 120
    frame[80:112, 120:168] = target

    matches = scan_image(template, frame)

    assert len(matches) == 1
    assert matches[0].rect == (120, 80, 48, 32)


@pytest.mark.parametrize("size", [16, 24])
def test_small_scaled_icon_survives_coarse_dimension_collisions(size):
    # At 1080p both 1.425x/1.5x (16 px) or 1.45x/1.5x (24 px)
    # collapse to the same coarse dimensions. Keeping the first scale alone
    # loses the exact target even though the coarse search finds its region.
    template = np.random.default_rng(1).integers(
        0, 256, (size, size, 3), dtype=np.uint8,
    )
    target_size = round(size * 1.5)
    target = cv2.resize(template, (target_size, target_size),
                        interpolation=cv2.INTER_LINEAR)
    frame = np.full((1080, 1920, 3), 40, dtype=np.uint8)
    frame[411:411 + target_size, 713:713 + target_size] = target

    matches = scan_image(template, frame)

    assert len(matches) == 1
    assert matches[0].rect == (713, 411, target_size, target_size)


def test_small_brightness_change_preserves_real_target():
    template = np.random.default_rng(12).integers(
        30, 200, (32, 48, 3), dtype=np.uint8,
    )
    frame = np.full((100, 160, 3), 220, dtype=np.uint8)
    frame[30:62, 80:128] = template + 5

    matches = scan_image(template, frame)

    assert len(matches) == 1
    assert matches[0].rect == (80, 30, 48, 32)


def test_strict_threshold_distinguishes_similar_button_text():
    def button(text):
        image = np.full((48, 128, 3), (30, 170, 240), dtype=np.uint8)
        cv2.rectangle(image, (3, 3), (124, 44), (10, 40, 70), 2)
        cv2.putText(image, text, (12, 33), cv2.FONT_HERSHEY_SIMPLEX,
                    .8, (250, 250, 250), 2)
        return image

    template = button("START")
    frame = np.full((180, 280, 3), 40, dtype=np.uint8)
    frame[50:98, 70:198] = button("STARE")

    assert scan_image(template, frame, threshold=.98) == []


def test_target_move_is_found_on_first_frame_after_previous_location_empties():
    template = np.random.default_rng(15).integers(
        0, 256, (24, 40, 3), dtype=np.uint8,
    )
    frame = np.full((240, 480, 3), 40, dtype=np.uint8)
    frame[30:54, 40:80] = template
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95)],
        capture_fn=lambda: frame,
    )
    assert engine.scan_once(trigger=False)[0].rect == (40, 30, 40, 24)
    frame.fill(40)
    frame[180:204, 370:410] = template

    matches = engine.scan_once(trigger=False)

    assert engine.last_error is None
    assert len(matches) == 1
    assert matches[0].rect == (370, 180, 40, 24)


def test_excluded_previews_do_not_fill_coarse_candidate_limit():
    rng = np.random.default_rng(2)
    template = rng.integers(0, 256, (48, 120, 3), dtype=np.uint8)
    frame = np.full((1080, 1920, 3), 40, dtype=np.uint8)
    for index in range(18):
        y = 24 + (index // 6) * 120
        x = 24 + (index % 6) * 168
        frame[y:y + 48, x:x + 120] = template
    noise = rng.integers(-3, 4, template.shape)
    target = np.clip(template.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    frame[804:852, 1596:1716] = target
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95)],
        capture_fn=lambda: frame,
        exclude_regions=lambda: [(0, 0, 1200, 500)],
        immediate_click=True,
    )

    # auto_click is disabled: triggering exercises immediate search ordering
    # without issuing any real mouse input.
    matches = engine.scan_once(trigger=True)

    assert engine.last_error is None
    assert len(matches) == 1
    assert matches[0].rect == (1596, 804, 120, 48)


def test_scaled_color_pattern_is_detected_when_grayscale_erases_its_detail():
    template = np.full((24, 40, 3), (255, 0, 0), dtype=np.uint8)
    mask = np.random.default_rng(2).integers(0, 2, (24, 40)).astype(bool)
    template[mask] = (0, 0, 97)
    assert np.unique(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)).tolist() == [29]
    target = cv2.resize(template, (60, 36), interpolation=cv2.INTER_LINEAR)
    frame = np.full((360, 540, 3), 29, dtype=np.uint8)
    frame[180:216, 350:410] = target

    matches = scan_image(template, frame)

    assert len(matches) == 1
    assert matches[0].rect == (350, 180, 60, 36)


def test_reloading_template_replaces_cached_scaled_pixels():
    rng = np.random.default_rng(41)
    template = rng.integers(0, 256, (32, 48, 3), dtype=np.uint8)
    replacement = rng.integers(0, 256, (32, 48, 3), dtype=np.uint8)
    frame = np.full((200, 360, 3), 40, dtype=np.uint8)
    frame[70:118, 180:252] = cv2.resize(template, (72, 48))
    spec = TemplateSpec(image=template, threshold=.95)
    engine = VisionEngine([spec], capture_fn=lambda: frame)
    assert engine.scan_once(trigger=False)[0].rect == (180, 70, 72, 48)
    spec.image = replacement
    engine.reload_templates()

    assert engine.scan_once(trigger=False) == []
    frame[70:118, 180:252] = cv2.resize(replacement, (72, 48))
    matches = engine.scan_once(trigger=False)
    assert engine.last_error is None
    assert len(matches) == 1
    assert matches[0].rect == (180, 70, 72, 48)


def test_cached_location_obeys_new_exclusion_on_next_frame():
    template = np.random.default_rng(27).integers(
        0, 256, (32, 48, 3), dtype=np.uint8,
    )
    frame = np.full((240, 480, 3), 40, dtype=np.uint8)
    frame[30:62, 40:88] = template
    excluded = []
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95)],
        capture_fn=lambda: frame,
        exclude_regions=lambda: excluded,
    )
    assert engine.scan_once(trigger=False)[0].rect == (40, 30, 48, 32)
    frame[180:212, 370:418] = template
    excluded.append((0, 0, 140, 110))

    matches = engine.scan_once(trigger=False)

    assert len(matches) == 1
    assert matches[0].rect == (370, 180, 48, 32)


def test_multiple_occurrences_at_different_scales_remain_distinct():
    template = np.random.default_rng(23).integers(
        0, 256, (32, 48, 3), dtype=np.uint8,
    )
    frame = np.full((240, 480, 3), 40, dtype=np.uint8)
    frame[30:62, 40:88] = template
    frame[170:218, 370:442] = cv2.resize(template, (72, 48))
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95, max_matches=2)],
        capture_fn=lambda: frame,
    )

    matches = engine.scan_once(trigger=False)

    assert engine.last_error is None
    assert {match.rect for match in matches} == {
        (40, 30, 48, 32), (370, 170, 72, 48),
    }


@pytest.mark.parametrize("replacement", ["absent", "different_color"])
def test_cached_target_is_not_fired_after_disappearing_or_changing(replacement):
    template = np.random.default_rng(48).integers(
        20, 100, (32, 48, 3), dtype=np.uint8,
    )
    frame = np.full((240, 480, 3), 220, dtype=np.uint8)
    frame[70:102, 100:148] = template
    on_match = Mock()
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95)],
        capture_fn=lambda: frame,
        immediate_click=True,
        on_match=on_match,
    )
    assert len(engine.scan_once(trigger=True)) == 1
    on_match.assert_called_once()
    frame[70:102, 100:148] = 220 if replacement == "absent" else template + 120

    assert engine.scan_once(trigger=True) == []
    assert engine.last_error is None
    on_match.assert_called_once()


def test_scaled_template_cache_stays_within_budget_and_can_be_cleared():
    template = np.random.default_rng(32).integers(
        0, 256, (32, 48, 3), dtype=np.uint8,
    )
    frame = np.full((240, 480, 3), 40, dtype=np.uint8)
    engine = VisionEngine(
        [TemplateSpec(image=template, threshold=.95)],
        capture_fn=lambda: frame,
    )
    engine._image_cache_limit = 20 * 1024
    for scale in [1.5, .75, 1.25, .5, 2.0]:
        width, height = round(48 * scale), round(32 * scale)
        frame.fill(40)
        frame[70:70 + height, 100:100 + width] = cv2.resize(
            template, (width, height),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
        assert engine.scan_once(trigger=False)[0].rect == (100, 70, width, height)
        assert engine._image_cache_bytes == sum(
            item.nbytes for item in engine._image_cache.values()
        )
        assert engine._image_cache_bytes <= engine._image_cache_limit
    engine.clear_templates()
    assert engine._image_cache_bytes == 0
    assert not engine._image_cache


def test_black_template_does_not_match_gray_background():
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    frame = np.full((100, 140, 3), 80, dtype=np.uint8)

    assert scan_image(template, frame) == []
