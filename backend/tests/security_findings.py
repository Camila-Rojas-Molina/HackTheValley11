"""Every finding of the two adversarial review rounds (r1, r2) and the regression tests that pin its fix.

Keys are "r1#N" / "r2#N"; values name the fix commit and the tests ("file::test_function"). Round 2 fixed
findings in themed commits, so findings fixed together share that commit's tests. r1 #27 and #28 are not
referenced by any commit and have no entry (see UNREFERENCED).

tests/test_security_regressions.py checks that every finding has an entry and that every listed test
exists; tests/conftest.py marks the listed tests `security`, so `pytest -m security` runs exactly them.
"""
from __future__ import annotations

UNREFERENCED = ("r1#27", "r1#28")

_TWIN = ("f4f03e4", [
    "test_masking_twin_db.py::test_a_duress_squeeze_during_an_escalation_never_shows_duress_to_the_wearer",
    "test_masking_twin_db.py::test_a_pin_after_a_duress_squeeze_during_an_escalation_answers_like_after_a_plain_one",
    "test_masking_twin_db.py::test_the_record_after_the_duress_pin_matches_the_real_pin",
    "test_masking_twin_db.py::test_a_duress_squeeze_adds_nobody_to_her_record_while_the_alert_is_open",
    "test_masking_twin_db.py::test_an_unanswered_cover_check_in_escalates_like_the_twin_and_ignores_the_hidden_ladder",
    "test_masking_twin_db.py::test_a_device_left_unplugged_after_a_duress_pin_shares_the_lost_contact_notice_like_the_twin",
    "test_masking_twin_db.py::test_a_duress_ended_session_keeps_the_link_state_it_ended_with",
    "test_masking_twin_db.py::test_a_contact_kept_out_of_duress_alerts_sees_the_real_pin_world_after_the_duress_pin",
    "test_masking_twin_db.py::test_a_contact_kept_out_of_duress_alerts_sees_the_plain_squeeze_world_after_a_duress_squeeze",
    "test_duress_recipients.py::test_turning_a_no_response_alert_into_duress_shows_the_excluded_contact_only_the_twin",
    "test_duress_recipients.py::test_a_no_response_alert_turned_duress_reads_as_a_false_alarm_for_the_excluded_contact",
    "test_states.py::test_a_duress_squeeze_during_an_alert_masks_it_and_the_twin_keeps_the_alert_she_saw",
    "test_property_invariants.py::test_real_and_duress_pin_look_the_same_from_every_reachable_state",
    "test_property_db.py::test_real_and_duress_pin_are_byte_identical_after_any_prefix",
])
_CONTACT_INPUT = ("6e6bd2a", [
    "test_contact_security_db.py::test_removing_a_contact_ends_their_location_link",
    "test_contact_security_db.py::test_a_message_in_flight_when_the_contact_is_removed_is_never_sent",
    "test_contact_security_db.py::test_the_sender_never_sends_to_a_removed_contact",
    "test_contact_security_db.py::test_a_telegram_button_cannot_touch_another_wearers_alert",
    "test_contact_security_db.py::test_a_telegram_button_works_only_for_a_contact_told_about_the_alert",
    "test_contact_security_db.py::test_arming_with_another_wearers_or_an_unknown_device_is_a_404",
    "test_contact_security_db.py::test_two_arm_requests_at_once_give_one_session_and_a_409",
    "test_contact_security_db.py::test_one_contact_looping_ack_and_cant_reach_cannot_flood_the_others",
    "test_contact_security_db.py::test_a_second_acker_is_still_announced_to_the_others",
    "test_contact_security_db.py::test_input_the_database_cannot_store_is_rejected_not_a_500",
    "test_bridge.py::test_lines_with_control_characters_in_their_ids_are_dropped",
    "test_bridge.py::test_a_dropped_noisy_line_never_holds_up_the_next_event",
    "test_states.py::test_back_to_final_spends_the_renotify_budget_and_a_repeat_ack_is_not_announced",
    "test_contract_db.py::test_contract",
])
_ROBUST = ("1391949", [
    "test_robustness_db.py::test_a_retried_alert_is_never_delivered_after_the_false_alarm_that_closed_it",
    "test_robustness_db.py::test_a_failing_worker_never_unfires_a_timer_another_worker_fired",
    "test_robustness_db.py::test_a_deadline_that_keeps_failing_is_retried_until_it_fires",
    "test_robustness_db.py::test_an_update_with_an_id_below_the_stored_offset_is_still_processed_once",
    "test_robustness_db.py::test_the_offset_belongs_to_one_bot",
    "test_robustness_db.py::test_the_poller_follows_update_ids_that_restart_lower",
    "test_robustness_db.py::test_a_buffered_squeeze_from_before_the_prompt_does_not_answer_it",
    "test_robustness_db.py::test_a_live_squeeze_without_a_challenge_id_still_answers",
    "test_robustness_db.py::test_simulate_refuses_a_server_with_a_telegram_bot",
    "test_robustness_db.py::test_the_readme_starts_the_simulate_server_without_the_bot_token",
    "test_bridge.py::test_a_squeeze_carries_the_challenge_the_device_showed_when_she_made_it",
])
_TIMESERIES = ("eebb19d", [
    "test_timeseries_tsdb.py::test_toolkit_gaps_include_the_silence_before_and_after_the_hours_with_data",
    "test_timeseries_tsdb.py::test_toolkit_gaps_keep_a_samples_liveness_past_an_hour_boundary",
    "test_timeseries_tsdb.py::test_reseeding_the_synthetic_baseline_works_once_its_chunks_are_compressed",
    "test_timeseries_tsdb.py::test_a_backlog_older_than_the_refresh_window_reaches_every_rollup",
    "test_timeseries_tsdb.py::test_migrations_fall_back_to_plain_postgres_when_timescaledb_is_installed_but_not_preloaded",
])
_CARE = ("de2f392", [
    "test_care_db.py::test_snooze_the_push_budget_and_the_digest_take_effect_through_the_api",
    "test_care_rules.py::test_heart_rate_that_is_not_available_never_makes_a_worn_device_read_as_missing_data",
    "test_care_rules.py::test_seed_666_keeps_every_no_change_resident_out_of_the_digest",
    "test_care_rules.py::test_the_medication_raise_is_a_stated_threshold_on_every_card_it_raises",
    "test_care_vultr.py::test_a_vultr_briefing_keeps_the_record_context_and_the_data_line",
])

_SENSOR_MASK = ["test_masking_db.py::test_sensor_events_during_masked_duress_prompt_like_armed",
                "test_masking_db.py::test_heart_rate_rule_during_masked_duress_prompts_like_armed",
                "test_states.py::test_masked_duress_sensor_input_prompts_the_cover_only"]
_SCHEDULE_MASK = ["test_masking_db.py::test_masked_duress_keeps_the_check_in_schedule",
                  "test_masking_db.py::test_masked_duress_still_asks_home_safe_at_the_preset_end",
                  "test_states.py::test_masked_duress_keeps_the_twins_check_in_schedule",
                  "test_states.py::test_masked_duress_session_end_asks_home_safe"]
_ERASE = ["test_masking_db.py::test_delete_my_data_during_an_open_alert_keeps_the_contacts_link_and_matches_the_twin",
          "test_masking_db.py::test_delete_my_data_during_an_open_no_response_alert_keeps_the_location_link",
          "test_masking_db.py::test_chain_after_erase_is_checked_from_its_first_kept_row_and_still_catches_edits"]
_REARM = ["test_masking_db.py::test_arming_again_after_a_duress_end_starts_a_fresh_session"]
_OPERATOR_HIDDEN = ["test_masking_db.py::test_operator_entries_stay_hidden_while_a_masked_alert_is_open"]
_PRUNE = ["test_timeseries.py::test_plain_path_prunes_night_out_heart_rate_without_any_telemetry_batch"]
_HEAD = ["test_masking_db.py::test_export_head_hash_recomputes_from_the_visible_entries_for_both_pins"]

FINDINGS: dict[str, tuple[str, list[str]]] = {
    "r1#1": ("b8fc0a4", _SENSOR_MASK),
    "r1#2": ("3dbb8e8", _SCHEDULE_MASK),
    "r1#3": ("0aa8742", ["test_masking_db.py::test_wearer_input_during_masked_duress_is_answered_like_armed",
                         "test_states.py::test_masked_duress_wearer_input_matches_the_twin_and_still_tells_contacts"]),
    "r1#4": ("259daba", _REARM),
    "r1#5": ("9734adc", ["test_masking_db.py::test_a_pin_after_a_duress_end_answers_like_an_ended_session"]),
    "r1#6": ("e3b4c9d", ["test_masking_db.py::test_false_alarm_entry_is_identical_for_real_and_duress_pin"]),
    "r1#7": ("b610f31", ["test_masking_db.py::test_export_shows_ended_at_after_either_pin"] + _HEAD),
    "r1#8": ("1f9325a", ["test_masking_db.py::test_device_unplugged_after_a_duress_pin_messages_the_wearer_like_the_twin",
                         "test_states.py::test_masked_duress_link_notices_follow_the_twin"]),
    "r1#9": ("fef2cfe", _ERASE),
    "r1#10": ("dadf70e", _OPERATOR_HIDDEN),
    "r1#11": ("1f173f9", ["test_config.py::test_env_example_placeholders_are_treated_as_unset",
                          "test_config.py::test_a_server_started_from_env_example_rejects_the_placeholder_keys"]),
    "r1#12": ("6dc8490", ["test_config.py::test_access_log_lines_never_carry_capability_tokens",
                          "test_wiring_live.py::test_the_running_servers_access_log_never_holds_a_capability_token"]),
    "r1#13": ("00c2fce", ["test_masking_db.py::test_public_health_does_not_change_with_a_duress_pin"]),
    "r1#14": ("dadf70e", _OPERATOR_HIDDEN),
    "r1#15": ("225610f", _PRUNE),
    "r1#16": ("fef2cfe", _ERASE),
    "r1#17": ("a916b62", ["test_config.py::test_location_link_sealed_under_an_older_secret_answers_410_not_500",
                          "test_config.py::test_unset_app_secret_with_a_remote_database_logs_a_warning"]),
    "r1#18": ("7da5e4a", ["test_outbox_telegram_db.py::test_telegram_start_from_a_group_chat_binds_nothing"]),
    "r1#19": ("c1bb3ce", ["test_outbox_telegram_db.py::test_outbox_never_gives_up_on_a_network_outage",
                          "test_outbox_telegram_db.py::test_outbox_stops_retrying_a_permanent_rejection"]),
    "r1#20": ("6608535", ["test_outbox_telegram_db.py::test_contact_tap_racing_the_ack_timeout_is_not_lost",
                          "test_outbox_telegram_db.py::test_a_transient_database_error_leaves_the_update_for_a_retry",
                          "test_outbox_telegram_db.py::test_live_page_tap_racing_the_ack_timeout_is_not_lost",
                          "test_load_db.py::test_fifty_device_events_and_ten_contact_taps_at_once"]),
    "r1#21": ("98a32db", ["test_outbox_telegram_db.py::test_workers_wait_for_the_app_secret"]),
    "r1#22": ("cd20d92", ["test_outbox_telegram_db.py::test_poller_lock_of_a_vanished_client_is_released"]),
    "r1#23": ("83f35ee", ["test_flow_db.py::test_session_end_during_an_alert_still_asks_home_safe_afterwards"]),
    "r1#24": ("c8f5c82", ["test_outbox_telegram_db.py::test_rows_claimed_by_a_killed_sender_go_out_right_after_restart",
                          "test_outbox_telegram_db.py::test_a_late_release_cannot_touch_a_row_another_sender_claimed"]),
    "r1#25": ("225610f", _PRUNE),
    "r1#26": ("3b02e81", ["test_timeseries.py::test_zscore_rows_never_carry_nan_toolkit_one_minute_baseline"]),
    "r1#29": ("d1c70ed", ["test_timeseries.py::test_same_batch_id_from_two_senders_of_one_wearer_is_stored_twice"]),
    "r1#30": ("62972be", ["test_timeseries.py::test_zscore_for_one_wearer_reads_only_that_wearers_raw_rows"]),
    "r1#31": ("b7e3362", _HEAD),
    "r1#32": ("259daba", _REARM),
    "r1#33": ("0aa8742", ["test_masking_db.py::test_after_a_duress_end_wearer_input_gets_no_wearer_message"]),
    "r1#34": ("b8fc0a4", _SENSOR_MASK + _SCHEDULE_MASK),
    "r1#35": ("8afc5e7", ["test_flow_db.py::test_out_of_range_numbers_get_4xx_never_500",
                          "test_property_db.py::test_no_sensor_heart_rate_or_telemetry_input_ever_escalates"]),
    "r1#36": ("9a77368", ["test_care_vultr.py::test_every_text_the_care_wording_check_flags_is_refused_by_the_briefing_filter"]),
    "r1#37": ("6f20839", ["test_care_db.py::test_a_resident_whose_data_stopped_stays_on_the_alert_list",
                          "test_care_rules.py::test_missing_device_data_is_never_reported_as_routine_care"]),
    "r1#38": ("4ec03a1", ["test_docs.py::test_api_md_labels_the_sensor_thresholds_as_untested_assumptions"]),
    # The settle rule was later replaced by the heart-rate-after-standing rule (40fa02b), which keeps the check.
    "r1#39": ("83ea1df", ["test_care_rules.py::test_stand_hr_not_enough_data_text_states_the_rule_it_applies"]),
    "r1#40": ("fad4b80", ["test_vultr.py::test_cli_check_uses_a_card_the_rule_engine_produces",
                          "test_vultr.py::test_template_briefing_does_not_call_the_baseline_recent"]),
    "r1#41": ("28c5326", ["test_ops_scripts.py::test_simulate_runs_the_pcc_seed_after_the_kaze_scenarios"]),
    "r1#42": ("97aba4d", ["test_docs.py::test_readme_commands_use_the_project_venv_python"]),
}
for _n in (1, 2, 3, 4, 5, 6, 7, 8, 22, 23, 24):
    FINDINGS[f"r2#{_n}"] = _TWIN
for _n in (9, 10, 11, 12, 17, 26, 27, 28):
    FINDINGS[f"r2#{_n}"] = _CONTACT_INPUT
for _n in (13, 14, 15, 16, 25, 30):
    FINDINGS[f"r2#{_n}"] = _ROBUST
for _n in (18, 19, 20, 21, 34):
    FINDINGS[f"r2#{_n}"] = _TIMESERIES
for _n in (29, 31, 32, 33):
    FINDINGS[f"r2#{_n}"] = _CARE
FINDINGS["r2#35"] = ("8906784", ["test_timeseries.py::test_a_failed_backlog_refresh_never_fails_the_stored_batch",
                                 "test_timeseries_tsdb.py::test_a_backlog_older_than_the_refresh_window_reaches_every_rollup"])


def all_tests() -> set[str]:
    return {t for _, tests in FINDINGS.values() for t in tests}
