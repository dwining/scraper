"""Human-behaviour simulation utilities (FR-3).

Provides gradual mouse movement with noise, incremental random scrolling,
random delays, container scrolling (for the Instagram comment panel) and
the occasional "look around" re-read behaviour.

All time/px ranges are configurable but default to safe, natural values.
"""

from __future__ import annotations

import random
import time
from typing import Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains


def random_delay(sec_range: tuple[float, float] = (2, 6)) -> float:
    """Return a uniform random delay in ``[sec_range[0], sec_range[1]]`` seconds."""
    return random.uniform(float(sec_range[0]), float(sec_range[1]))


def sleep_random(sec_range: tuple[float, float] = (2, 6)) -> None:
    """Sleep for a uniform random duration within ``sec_range``."""
    time.sleep(random_delay(sec_range))


def human_move_to(driver, target, steps: int = 8) -> None:
    """Gradually move the mouse to ``target`` with small random noise.

    Uses ``ActionChains`` with multiple intermediate points (a noisy path,
    not a perfect straight line and not a single jump). Falls back to a
    plain ``move_to_element`` if coordinate computation fails.
    """
    steps = max(2, int(steps))

    try:
        # Ensure the element is visible inside the viewport first.
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            target,
        )
        time.sleep(random.uniform(0.2, 0.5))
        rect = target.rect
        el_x, el_y = float(rect["x"]), float(rect["y"])
        width, height = float(rect["width"]), float(rect["height"])
    except (WebDriverException, TypeError, KeyError):
        # Anything went wrong locating the element: move directly.
        ActionChains(driver).move_to_element(target).perform()
        return

    # Target centre and a random starting point offset from it.
    target_x = el_x + width / 2.0
    target_y = el_y + height / 2.0
    start_x = target_x + random.randint(-280, -100)
    start_y = target_y + random.randint(-200, -60)

    actions = ActionChains(driver)
    try:
        for i in range(1, steps + 1):
            t = i / float(steps)
            # Noise shrinks as we approach the target (easing).
            wiggle = max(2.0, 28.0 * (1.0 - t))
            px = start_x + (target_x - start_x) * t + random.uniform(-wiggle, wiggle)
            py = start_y + (target_y - start_y) * t + random.uniform(-wiggle, wiggle)
            # Move to an absolute point relative to the element's top-left corner.
            actions.move_to_element_with_offset(
                target,
                int(px - el_x),
                int(py - el_y),
            )
            actions.pause(random.uniform(0.03, 0.10))
        actions.move_to_element(target)
        actions.perform()
    except WebDriverException:
        try:
            ActionChains(driver).move_to_element(target).perform()
        except WebDriverException:
            pass


def human_click(
    driver,
    element,
    pre_delay_range: tuple[float, float] = (0.5, 1.5),
    post_delay_range: tuple[float, float] = (1.0, 2.0),
) -> None:
    """Move to ``element`` like a human, click it, and pause afterwards."""
    sleep_random(pre_delay_range)
    human_move_to(driver, element)
    try:
        ActionChains(driver).click(element).perform()
    except WebDriverException:
        # Element may have gone stale between move and click; retry once.
        try:
            element.click()
        except Exception:
            pass
    sleep_random(post_delay_range)


def human_scroll(
    driver,
    step_px_range: tuple[int, int] = (200, 600),
    min_steps: int = 2,
    max_steps: int = 6,
) -> None:
    """Scroll the page down incrementally with random px per step.

    Uses ``window.scrollBy`` so each step is small and non-deterministic.
    """
    n_steps = random.randint(int(min_steps), int(max_steps))
    for _ in range(n_steps):
        step = random.randint(int(step_px_range[0]), int(step_px_range[1]))
        try:
            driver.execute_script("window.scrollBy(0, arguments[0]);", step)
        except WebDriverException:
            return
        sleep_random((0.4, 1.2))


def scroll_container(
    driver,
    container_element,
    step_px_range: tuple[int, int] = (300, 800),
    min_steps: int = 1,
    max_steps: int = 4,
) -> None:
    """Scroll *inside* a scrollable container (e.g. Instagram comment panel)."""
    n_steps = random.randint(int(min_steps), int(max_steps))
    for _ in range(n_steps):
        step = random.randint(int(step_px_range[0]), int(step_px_range[1]))
        try:
            driver.execute_script(
                "arguments[0].scrollBy(0, arguments[1]);",
                container_element,
                step,
            )
        except WebDriverException:
            return
        sleep_random((0.4, 1.2))


def look_around(driver, look_back_px: tuple[int, int] = (100, 250)) -> None:
    """Small scroll up then back down, simulating a person re-reading content."""
    amount = random.randint(int(look_back_px[0]), int(look_back_px[1]))
    try:
        driver.execute_script("window.scrollBy(0, arguments[0]);", -amount)
        sleep_random((0.3, 0.8))
        driver.execute_script("window.scrollBy(0, arguments[0]);", amount)
        sleep_random((0.3, 0.8))
    except WebDriverException:
        pass
