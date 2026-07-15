from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "display-layout-manager.py"
SPEC = importlib.util.spec_from_file_location("display_layout_manager_safety", SCRIPT)
dlm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dlm
assert SPEC.loader is not None
SPEC.loader.exec_module(dlm)


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.fired = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if self.started and not self.cancelled and not self.fired:
            self.fired = True
            self.callback()


class FakeTimerFactory:
    def __init__(self):
        self.timers: list[FakeTimer] = []

    def __call__(self, delay, callback):
        timer = FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer

    def pending(self) -> list[FakeTimer]:
        return [
            timer for timer in self.timers
            if timer.started and not timer.cancelled and not timer.fired
        ]

    def fire_next(self) -> FakeTimer:
        timer = min(self.pending(), key=lambda candidate: candidate.delay)
        timer.fire()
        return timer

    def fire_delay(self, delay: float) -> FakeTimer:
        timer = next(
            candidate for candidate in self.pending()
            if candidate.delay == delay
        )
        timer.fire()
        return timer


def make_snapshot(
    *,
    active=(),
    online=(),
    builtin=(),
    sleeping=(),
    connected=None,
    metadata=None,
    zero_metadata=(),
):
    active = set(active)
    online = set(online)
    builtin = set(builtin)
    sleeping = set(sleeping)
    if connected is None:
        connected = active | online | builtin | sleeping
    connected = set(connected)
    metadata = metadata or {}
    zero_metadata = set(zero_metadata)
    ids = active | online | builtin | sleeping | connected
    return dlm.DisplaySafetySnapshot(tuple(
        dlm.DisplaySafetyDisplay(
            cg_id=display_id,
            serial=(
                0 if display_id in zero_metadata
                else metadata.get(display_id, (0x610, 0x100 + display_id, 1000 + display_id))[2]
            ),
            vendor=(
                0 if display_id in zero_metadata
                else metadata.get(display_id, (0x610, 0x100 + display_id, 1000 + display_id))[0]
            ),
            model=(
                0 if display_id in zero_metadata
                else metadata.get(display_id, (0x610, 0x100 + display_id, 1000 + display_id))[1]
            ),
            is_builtin=display_id in builtin,
            is_active=display_id in active,
            is_online=display_id in online,
            is_asleep=display_id in sleeping,
            is_connected=display_id in connected,
        )
        for display_id in sorted(ids)
    ))


class TimerBatchTests(unittest.TestCase):
    def test_firing_one_attempt_does_not_cancel_sibling_retries(self):
        factory = FakeTimerFactory()
        batch = dlm._TimerBatch(factory)
        callback = Mock()

        batch.schedule((2, 5, 10), callback)
        timers = list(factory.timers)
        timers[0].fire()

        callback.assert_called_once_with()
        self.assertFalse(timers[1].cancelled)
        self.assertFalse(timers[2].cancelled)
        timers[1].fire()
        timers[2].fire()
        self.assertEqual(callback.call_count, 3)

    def test_new_generation_cancels_only_the_previous_generation(self):
        factory = FakeTimerFactory()
        batch = dlm._TimerBatch(factory)

        batch.schedule((2, 5, 10), Mock())
        previous = list(factory.timers)
        replacement = Mock()
        batch.schedule((1,), replacement)

        self.assertTrue(all(timer.cancelled for timer in previous))
        factory.fire_next()
        replacement.assert_called_once_with()


class DisplaySafetyControllerTests(unittest.TestCase):
    def _controller(
        self,
        query,
        *,
        clamshell=None,
        operation_lock=None,
        interval=30,
        power_source=dlm.PowerSourceState.AC,
    ):
        factory = FakeTimerFactory()
        reenable = Mock(return_value=dlm.ReenableResult(True, "cgs", [1]))
        reenable_fallback = Mock(
            return_value=dlm.ReenableResult(True, "displayplacer", [1]),
        )
        schedule_layout = Mock()
        logger = Mock()
        controller = dlm.DisplaySafetyController(
            enabled=True,
            watchdog_interval=interval,
            clamshell=clamshell or dlm.ClamshellState(True, False, True),
            operation_lock=operation_lock or threading.Lock(),
            power_source=power_source,
            snapshot_query=query,
            reenable=reenable,
            reenable_fallback=reenable_fallback,
            schedule_layout=schedule_layout,
            logger=logger,
            timer_factory=factory,
        )
        return controller, factory, reenable, schedule_layout, logger

    @staticmethod
    def _prime(controller, snapshot):
        controller._evaluate_snapshot(snapshot, "prime", schedule=False)

    @staticmethod
    def _external_only(*, external_ids=(2, 3), metadata=None):
        ids = {1, *external_ids}
        return make_snapshot(
            active=set(external_ids),
            online=ids,
            builtin={1},
            connected=ids,
            metadata=metadata,
        )

    def test_partial_loss_recovers_internal_despite_active_ghost(self):
        baseline = self._external_only(external_ids=(2, 3))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        controller, timers, reenable, schedule_layout, _logger = (
            self._controller(query)
        )
        self._prime(controller, baseline)

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()  # immediate fresh observation
        timers.fire_delay(2.0)  # debounced recovery

        reenable.assert_called_once_with([1])
        schedule_layout.assert_called_once_with("display-remove")
        self.assertFalse(controller.pending_undock)

    def test_cached_zero_metadata_internal_is_targeted_when_row_disappears(self):
        baseline = make_snapshot(
            active={2, 3},
            online={1, 2, 3},
            builtin={1},
            connected={1, 2, 3},
            zero_metadata={1},
        )
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2},
            online={1, 2},
            connected={1, 2},
            zero_metadata={1},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_DISABLED_FLAG,
        )
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_reused_internal_contextual_id_is_not_targeted_as_internal(self):
        baseline = self._external_only(external_ids=(2, 3))
        internal_identity = (0x610, 0x101, 1001)
        renumbered = make_snapshot(
            active={2, 3},
            online={2, 3, 5},
            builtin={5},
            connected={2, 3, 5},
            metadata={5: internal_identity},
        )
        reused_id = (0x9999, 0x8888, 77)
        ghost = make_snapshot(
            active={1, 2},
            online={1, 2},
            connected={1, 2},
            metadata={1: reused_id},
        )
        recovered = make_snapshot(
            active={2, 5},
            online={2, 5},
            builtin={5},
            connected={2, 5},
            metadata={5: internal_identity},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        self._prime(controller, renumbered)

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([5])
        self.assertFalse(controller.pending_undock)

    def test_ac_to_battery_recovers_when_single_external_is_still_a_ghost(self):
        baseline = self._external_only(external_ids=(2,))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.update_power_source(
            dlm.PowerSourceState.BATTERY,
            "power-source-change",
        )
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_ac_to_battery_pending_state_is_sticky_if_ac_returns(self):
        baseline = self._external_only(external_ids=(2,))
        controller, _timers, _reenable, _schedule, _logger = self._controller(
            Mock(),
        )
        self._prime(controller, baseline)

        controller.update_power_source(
            dlm.PowerSourceState.BATTERY,
            "power-source-change",
        )
        controller.update_power_source(
            dlm.PowerSourceState.AC,
            "power-source-change",
        )

        self.assertTrue(controller.pending_undock)

    def test_closed_lid_sleep_attempt_does_not_require_causes_sleep(self):
        baseline = self._external_only(external_ids=(2,))
        closed_ghost = make_snapshot(active={2}, online={2}, connected={2})
        # A closed MacBook may omit both the built-in marker and its metadata.
        closed_recovered = make_snapshot(
            active={1, 2}, online={1, 2}, connected={1, 2}, zero_metadata={1},
        )
        query = Mock(side_effect=[closed_ghost, closed_recovered])
        controller, _timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        controller.update_clamshell(
            dlm.ClamshellState(True, True, False),
            "lid-close",
        )
        controller.update_power_source(
            dlm.PowerSourceState.BATTERY,
            "power-source-change",
        )

        controller.prepare_for_sleep()

        reenable.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_wake_detects_missed_ac_to_battery_and_recovers_first(self):
        baseline = self._external_only(external_ids=(2,))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        controller.update_clamshell(
            dlm.ClamshellState(True, True, False),
            "lid-close",
        )
        controller.prepare_for_sleep()

        controller.handle_wake(
            dlm.ClamshellState(True, False, True),
            dlm.PowerSourceState.BATTERY,
        )
        timers.fire_delay(0.0)

        reenable.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_transient_remove_add_with_complete_baseline_does_not_recover(self):
        baseline = self._external_only(external_ids=(2, 3))
        query = Mock(return_value=baseline)
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()
        self.assertFalse(controller.pending_undock)
        timers.fire_delay(2.0)

        reenable.assert_not_called()

    def test_sleeping_online_baseline_member_is_still_present(self):
        baseline = self._external_only(external_ids=(2, 3))
        sleeping = make_snapshot(
            active={2},
            online={1, 2, 3},
            builtin={1},
            sleeping={3},
            connected={1, 2, 3},
        )
        query = Mock(return_value=sleeping)
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.request_check("watchdog")
        timers.fire_next()

        reenable.assert_not_called()
        self.assertFalse(controller.pending_undock)

    def test_inactive_online_external_is_not_part_of_automatic_baseline(self):
        topology = make_snapshot(
            active={2},
            online={1, 2, 3},
            builtin={1},
            connected={1, 2, 3},
        )
        controller, _timers, _reenable, _schedule, _logger = self._controller(
            Mock(),
        )

        self._prime(controller, topology)

        self.assertEqual(sum(controller.external_baseline.values()), 1)
        self.assertEqual(set(controller._baseline_ids), {2})

    def test_watchdog_detects_missing_baseline_without_callback(self):
        baseline = self._external_only(external_ids=(2, 3))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.request_check("watchdog")
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])

    def test_contextual_id_reassignment_preserves_identity_baseline(self):
        original_metadata = {
            2: (0x1111, 0x2222, 10),
            3: (0x1111, 0x3333, 20),
        }
        reassigned_metadata = {
            5: original_metadata[2],
            6: original_metadata[3],
        }
        baseline = self._external_only(
            external_ids=(2, 3), metadata=original_metadata,
        )
        reassigned = make_snapshot(
            active={5, 6},
            online={1, 5, 6},
            builtin={1},
            connected={1, 5, 6},
            metadata=reassigned_metadata,
        )
        query = Mock(return_value=reassigned)
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        expected_baseline = controller.external_baseline

        controller.request_check("watchdog")
        timers.fire_next()

        self.assertEqual(controller.external_baseline, expected_baseline)
        self.assertEqual(set(controller._baseline_ids), {5, 6})
        self.assertFalse(controller.pending_undock)
        reenable.assert_not_called()

    def test_duplicate_identity_counts_detect_one_missing_monitor(self):
        duplicate = (0x1234, 0x5678, 99)
        metadata = {2: duplicate, 3: duplicate}
        baseline = self._external_only(
            external_ids=(2, 3), metadata=metadata,
        )
        one_remaining = make_snapshot(
            active={2}, online={2}, connected={2}, metadata={2: duplicate},
        )
        query = Mock(return_value=one_remaining)
        controller, timers, _reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.request_check("watchdog")
        timers.fire_next()

        self.assertTrue(controller.pending_undock)
        self.assertEqual(sum(controller.external_baseline.values()), 2)

    def test_intentional_single_monitor_layout_adopts_only_enabled_external(self):
        baseline = self._external_only(external_ids=(2, 3, 4))
        settled = make_snapshot(
            active={2},
            online={1, 2, 3, 4},
            builtin={1},
            connected={1, 2, 3, 4},
        )
        query = Mock(return_value=settled)
        controller, timers, _reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        intent = dlm.DisplayOperationIntent(
            "Center-Only",
            expected_enabled_ids=frozenset({2}),
            expected_disabled_ids=frozenset({1, 3, 4}),
        )

        controller.begin_layout_operation(intent)
        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_DISABLED_FLAG,
        )
        controller.note_display_reconfiguration(
            4, dlm._CG_DISPLAY_DISABLED_FLAG,
        )
        self.assertFalse(controller.pending_undock)
        controller.finish_layout_operation(intent, 0)
        timers.fire_delay(2.0)

        identity = settled.by_id[2].identity
        self.assertEqual(controller.external_baseline, {identity: 1})
        controller.note_display_reconfiguration(
            2, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        self.assertTrue(controller.pending_undock)

    def test_first_layout_refreshes_identity_snapshot_before_baseline(self):
        before = self._external_only(external_ids=(2, 3))
        settled = make_snapshot(
            active={2},
            online={1, 2, 3},
            builtin={1},
            connected={1, 2, 3},
        )
        query = Mock(side_effect=[before, settled])
        controller, timers, _reenable, _schedule, _logger = self._controller(query)
        intent = dlm.DisplayOperationIntent(
            "Center-Only",
            expected_enabled_ids=frozenset({2}),
            expected_disabled_ids=frozenset({1, 3}),
        )

        controller.begin_layout_operation(intent)
        controller.finish_layout_operation(intent, 0)
        timers.fire_delay(2.0)

        self.assertEqual(
            controller.external_baseline,
            {before.by_id[2].identity: 1},
        )

    def test_expected_enabled_loss_arms_during_first_external_only_layout(self):
        both_active = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        controller, _timers, _reenable, _schedule, _logger = self._controller(
            Mock(),
        )
        self._prime(controller, both_active)
        intent = dlm.DisplayOperationIntent(
            "External-Only",
            expected_enabled_ids=frozenset({2}),
            expected_disabled_ids=frozenset({1}),
        )
        controller.begin_layout_operation(intent)

        controller.note_display_reconfiguration(
            2, dlm._CG_DISPLAY_REMOVE_FLAG,
        )

        self.assertTrue(controller.pending_undock)

    def test_non_disabling_layout_does_not_claim_external_only_baseline(self):
        both_active = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(return_value=both_active)
        controller, timers, _reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, both_active)
        intent = dlm.DisplayOperationIntent(
            "External-Only",
            expected_enabled_ids=frozenset({2}),
            expected_disabled_ids=frozenset({1}),
        )

        controller.begin_layout_operation(intent)
        controller.finish_layout_operation(intent, 0)
        timers.fire_delay(2.0)

        self.assertEqual(controller.external_baseline, {})
        self.assertFalse(controller.pending_undock)

    def test_snapshot_and_reset_failures_remain_retryable(self):
        baseline = self._external_only(external_ids=(2, 3))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[None, None, ghost, ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)
        reenable.side_effect = [
            dlm.ReenableResult(False, "cgs", [1], failed_stage="complete"),
            dlm.ReenableResult(True, "cgs", [1]),
        ]

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()  # failed event snapshot
        timers.fire_delay(2.0)  # failed recovery snapshot
        self.assertTrue(controller.pending_undock)
        timers.fire_delay(3.0)  # API failure and failed verification
        self.assertTrue(controller.pending_undock)
        timers.fire_delay(5.0)  # retry succeeds and verifies

        self.assertEqual(reenable.call_count, 2)
        self.assertFalse(controller.pending_undock)

    def test_cgs_success_without_activation_uses_displayplacer_fallback(self):
        baseline = self._external_only(external_ids=(2, 3))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, ghost, ghost, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, baseline)

        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])
        controller._reenable_fallback.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_busy_display_operation_uses_remaining_retry(self):
        baseline = self._external_only(external_ids=(2, 3))
        ghost = make_snapshot(active={2}, online={2}, connected={2})
        recovered = make_snapshot(
            active={1, 2}, online={1, 2}, builtin={1}, connected={1, 2},
        )
        query = Mock(side_effect=[ghost, ghost, recovered])
        operation_lock = threading.Lock()
        controller, timers, reenable, _schedule, _logger = self._controller(
            query, operation_lock=operation_lock,
        )
        self._prime(controller, baseline)
        controller.note_display_reconfiguration(
            3, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        timers.fire_next()
        operation_lock.acquire()
        timers.fire_delay(2.0)
        reenable.assert_not_called()
        operation_lock.release()

        timers.fire_delay(3.0)

        reenable.assert_called_once_with([1])
        self.assertFalse(controller.pending_undock)

    def test_no_baseline_zero_active_fallback_recovers_internal(self):
        internal_active = make_snapshot(
            active={1}, online={1}, builtin={1}, connected={1},
        )
        stranded = make_snapshot(online={1}, builtin={1}, connected={1})
        recovered = internal_active
        query = Mock(side_effect=[stranded, stranded, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, internal_active)

        controller.request_check("watchdog")
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])

    def test_anonymous_virtual_display_does_not_block_physical_fallback(self):
        internal_active = make_snapshot(
            active={1}, online={1}, builtin={1}, connected={1},
        )
        stranded = make_snapshot(
            active={99},
            online={1, 99},
            builtin={1},
            connected={1, 99},
            zero_metadata={99},
        )
        recovered = make_snapshot(
            active={1, 99},
            online={1, 99},
            builtin={1},
            connected={1, 99},
            zero_metadata={99},
        )
        query = Mock(side_effect=[stranded, stranded, recovered])
        controller, timers, reenable, _schedule, _logger = self._controller(query)
        self._prime(controller, internal_active)

        controller.request_check("watchdog")
        timers.fire_next()
        timers.fire_delay(2.0)

        reenable.assert_called_once_with([1])

    def test_no_clamshell_hardware_keeps_safety_inactive(self):
        query = Mock()
        controller, timers, reenable, _schedule, _logger = self._controller(
            query,
            clamshell=dlm.ClamshellState(False),
        )

        controller.start()

        self.assertFalse(controller.active)
        query.assert_not_called()
        reenable.assert_not_called()
        self.assertEqual(timers.pending(), [])

    def test_interval_zero_disables_watchdog_but_keeps_startup_check(self):
        active = make_snapshot(active={1}, online={1}, builtin={1}, connected={1})
        query = Mock(return_value=active)
        controller, timers, _reenable, _schedule, _logger = self._controller(
            query,
            interval=0,
        )

        controller.start()
        self.assertIsNone(controller._watchdog_thread)
        self.assertEqual(len(timers.pending()), 1)
        controller.stop()
        self.assertEqual(timers.pending(), [])

    def test_stop_terminates_running_watchdog_thread(self):
        query = Mock()
        controller, _timers, _reenable, _schedule, _logger = self._controller(
            query,
            interval=1,
        )

        controller.start()
        thread = controller._watchdog_thread
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        controller.stop()

        self.assertIsNone(controller._watchdog_thread)
        self.assertFalse(thread.is_alive())


class DisplaySafetySupportTests(unittest.TestCase):
    def test_clamshell_message_bits_are_decoded(self):
        self.assertEqual(
            dlm._decode_clamshell_message(0),
            dlm.ClamshellState(True, False, False),
        )
        self.assertEqual(
            dlm._decode_clamshell_message(1),
            dlm.ClamshellState(True, True, False),
        )
        self.assertEqual(
            dlm._decode_clamshell_message(3),
            dlm.ClamshellState(True, True, True),
        )

    def test_system_will_sleep_is_acknowledged_when_recovery_raises(self):
        prepare = Mock(side_effect=RuntimeError("reset failed"))
        acknowledge = Mock()
        logger = Mock()

        dlm._handle_system_will_sleep(prepare, acknowledge, logger)

        prepare.assert_called_once_with()
        acknowledge.assert_called_once_with()
        self.assertIn("reset failed", logger.call_args.args[0])

    def test_suppression_does_not_hide_display_loss_from_safety(self):
        controller = Mock()
        schedule_layout = Mock()

        dlm._route_display_reconfiguration(
            42,
            dlm._CG_DISPLAY_REMOVE_FLAG,
            suppressed=True,
            safety_controller=controller,
            schedule_layout=schedule_layout,
        )

        controller.note_display_reconfiguration.assert_called_once_with(
            42, dlm._CG_DISPLAY_REMOVE_FLAG,
        )
        schedule_layout.assert_not_called()

    def test_unsuppressed_add_event_schedules_layout_without_safety_reset(self):
        controller = Mock()
        schedule_layout = Mock()

        dlm._route_display_reconfiguration(
            77,
            dlm._CG_DISPLAY_ADD_FLAG,
            suppressed=False,
            safety_controller=controller,
            schedule_layout=schedule_layout,
        )

        controller.note_display_reconfiguration.assert_called_once_with(
            77, dlm._CG_DISPLAY_ADD_FLAG,
        )
        schedule_layout.assert_called_once_with()

    def test_power_source_names_are_decoded(self):
        self.assertEqual(
            dlm._power_source_state_from_text("AC Power"),
            dlm.PowerSourceState.AC,
        )
        self.assertEqual(
            dlm._power_source_state_from_text("Battery Power"),
            dlm.PowerSourceState.BATTERY,
        )
        self.assertEqual(
            dlm._power_source_state_from_text("not available"),
            dlm.PowerSourceState.UNKNOWN,
        )

    def test_unavailable_power_source_api_returns_unknown(self):
        self.assertEqual(
            dlm._read_power_source_state(object(), object()),
            dlm.PowerSourceState.UNKNOWN,
        )

    def _load_options(self, options_yaml: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text(
                f"options:\n{options_yaml}displays: []\nlayouts: []\n",
            )
            return dlm.load_config(path)[2]

    def test_display_safety_config_defaults_to_enabled_and_30_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text("displays: []\nlayouts: []\n")
            options = dlm.load_config(path)[2]

        self.assertTrue(options.enable_display_safety)
        self.assertEqual(options.display_watchdog_interval, 30)

    def test_display_safety_can_be_disabled_or_run_without_watchdog(self):
        options = self._load_options(
            "  enable-display-safety: false\n"
            "  display-watchdog-interval: 0\n",
        )

        self.assertFalse(options.enable_display_safety)
        self.assertEqual(options.display_watchdog_interval, 0)

    def test_invalid_display_safety_options_are_rejected(self):
        invalid_values = [
            "  enable-display-safety: yes\n",
            "  display-watchdog-interval: -1\n",
            "  display-watchdog-interval: 1.5\n",
            "  display-watchdog-interval: true\n",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(dlm.ConfigError):
                    self._load_options(value)

    def test_options_must_be_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text("options: []\ndisplays: []\nlayouts: []\n")
            with self.assertRaises(dlm.ConfigError):
                dlm.load_config(path)


if __name__ == "__main__":
    unittest.main()
