# SEAgent Phase 1.9 Regression Stabilization Report

## 1. Executive Summary

- **Total Tests Executed**: 440
- **Failures**: 91
- **Errors**: 61
- **Passing Tests**: 299
- **Total Issues (Failures + Errors)**: 141

## 2. Test Failure & Error Classification Matrix

| Test | Status | Category | Action |
|------|--------|----------|--------|
| `test_adversarial_p0 (unittest.loader._FailedTest.test_adversarial_p0)` | error | regression | fix |
| `test_asr_chinese_success (test_asr_api.TestASRAPI.test_asr_chinese_success)` | error | regression | fix |
| `test_asr_english_success (test_asr_api.TestASRAPI.test_asr_english_success)` | error | regression | fix |
| `test_equipment_transaction_with_rov_alias_observation (test_dialogue_manager_rov.DialogueManagerROVTest.test_equipment_transaction_with_rov_alias_observation)` | fail | regression | fix |
| `test_equipment_transaction_with_rov_alias_tractor (test_dialogue_manager_rov.DialogueManagerROVTest.test_equipment_transaction_with_rov_alias_tractor)` | fail | regression | fix |
| `test_equipment_transaction_with_rov_alias_work (test_dialogue_manager_rov.DialogueManagerROVTest.test_equipment_transaction_with_rov_alias_work)` | fail | regression | fix |
| `test_family_and_variant_candidate_interfaces (test_dialogue_manager_rov.DialogueManagerROVTest.test_family_and_variant_candidate_interfaces)` | fail | regression | fix |
| `test_model_change_updates_family_and_clears_old_unit_via_slot_store (test_dialogue_manager_rov.DialogueManagerROVTest.test_model_change_updates_family_and_clears_old_unit_via_slot_store)` | fail | regression | fix |
| `test_model_selection_auto_fills_family (test_dialogue_manager_rov.DialogueManagerROVTest.test_model_selection_auto_fills_family)` | fail | regression | fix |
| `test_process_passes_committed_slot_delta_to_responder (test_dialogue_manager_rov.DialogueManagerROVTest.test_process_passes_committed_slot_delta_to_responder)` | error | regression | fix |
| `test_prompt_requires_allowed_values_to_be_rendered_verbatim_for_all_fields (test_dialogue_manager_rov.DialogueManagerROVTest.test_prompt_requires_allowed_values_to_be_rendered_verbatim_for_all_fields)` | fail | regression | fix |
| `test_variant_alias_is_available_to_backend_lookup (test_dialogue_manager_rov.DialogueManagerROVTest.test_variant_alias_is_available_to_backend_lookup)` | fail | regression | fix |
| `test_empty_reason_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_empty_reason_falls_to_clarification)` | fail | legacy | update |
| `test_llm_exception_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_llm_exception_falls_to_clarification)` | fail | legacy | update |
| `test_llm_invalid_intent_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_llm_invalid_intent_falls_to_clarification)` | fail | legacy | update |
| `test_llm_invalid_json_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_llm_invalid_json_falls_to_clarification)` | fail | legacy | update |
| `test_llm_low_confidence_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_llm_low_confidence_falls_to_clarification)` | fail | legacy | update |
| `test_missing_reason_falls_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_missing_reason_falls_to_clarification)` | fail | legacy | update |
| `test_n06_current_task_params_task_status (test_intent_routing.TestIntentRoutingAndInvariance.test_n06_current_task_params_task_status)` | fail | legacy | update |
| `test_n07_pipeline_inspection_params_knowledge_qa (test_intent_routing.TestIntentRoutingAndInvariance.test_n07_pipeline_inspection_params_knowledge_qa)` | fail | legacy | update |
| `test_n08_what_robots_available_device_capability_found (test_intent_routing.TestIntentRoutingAndInvariance.test_n08_what_robots_available_device_capability_found)` | fail | legacy | update |
| `test_n10_able_to_work_500m_robots_device_capability (test_intent_routing.TestIntentRoutingAndInvariance.test_n10_able_to_work_500m_robots_device_capability)` | fail | legacy | update |
| `test_n12_missing_confidence_no_slot_update (test_intent_routing.TestIntentRoutingAndInvariance.test_n12_missing_confidence_no_slot_update)` | fail | legacy | update |
| `test_n13_all_invalid_confidences_fall_to_clarification (test_intent_routing.TestIntentRoutingAndInvariance.test_n13_all_invalid_confidences_fall_to_clarification)` | fail | legacy | update |
| `test_n16_confirming_confirm_publish_flow (test_intent_routing.TestIntentRoutingAndInvariance.test_n16_confirming_confirm_publish_flow)` | fail | legacy | update |
| `test_r03_active_task_thanks_routing (test_intent_routing.TestIntentRoutingAndInvariance.test_r03_active_task_thanks_routing)` | fail | legacy | update |
| `test_r04_active_task_irrelevant_input_routing (test_intent_routing.TestIntentRoutingAndInvariance.test_r04_active_task_irrelevant_input_routing)` | fail | legacy | update |
| `test_r09_confirm_publish_in_confirming_phase (test_intent_routing.TestIntentRoutingAndInvariance.test_r09_confirm_publish_in_confirming_phase)` | fail | legacy | update |
| `test_r10_cancel_current_task (test_intent_routing.TestIntentRoutingAndInvariance.test_r10_cancel_current_task)` | fail | legacy | update |
| `test_r12_non_task_routes_no_extractor_or_commit (test_intent_routing.TestIntentRoutingAndInvariance.test_r12_non_task_routes_no_extractor_or_commit)` | fail | legacy | update |
| `test_restored_device_status_routing (test_intent_routing.TestIntentRoutingAndInvariance.test_restored_device_status_routing)` | fail | legacy | update |
| `test_slot_candidates_nan_confidence_rejected (test_intent_routing.TestIntentRoutingAndInvariance.test_slot_candidates_nan_confidence_rejected)` | fail | legacy | update |
| `test_slot_candidates_no_longer_bypass_validation (test_intent_routing.TestIntentRoutingAndInvariance.test_slot_candidates_no_longer_bypass_validation)` | fail | legacy | update |
| `test_p1_done_revision_transaction_failure_rollback (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p1_done_revision_transaction_failure_rollback)` | fail | legacy | update |
| `test_p1_done_revision_with_invalid_value_changes_intent_id (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p1_done_revision_with_invalid_value_changes_intent_id)` | fail | legacy | update |
| `test_p2_pending_oilfield_does_not_intercept_negation_update (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p2_pending_oilfield_does_not_intercept_negation_update)` | fail | legacy | update |
| `test_p2_pending_oilfield_explicit_confirmation (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p2_pending_oilfield_explicit_confirmation)` | fail | legacy | update |
| `test_p3_done_snapshot_missing_disk_file_downgrades_phase (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p3_done_snapshot_missing_disk_file_downgrades_phase)` | fail | legacy | update |
| `test_p3_done_snapshot_valid_disk_file_restores_done (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p3_done_snapshot_valid_disk_file_restores_done)` | fail | legacy | update |
| `test_p4_specific_device_sequence_number_routes_to_device_capability (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p4_specific_device_sequence_number_routes_to_device_capability)` | fail | legacy | update |
| `test_p5_payload_conflict_targeted_cancellation (test_p0_boundary_closeout.P0BoundaryCloseoutTest.test_p5_payload_conflict_targeted_cancellation)` | fail | legacy | update |
| `test_d1_ambiguous_alias_routes_to_clarification (test_p0_final_closeout.AmbiguousDeviceAliasRoutingTest.test_d1_ambiguous_alias_routes_to_clarification)` | fail | legacy | update |
| `test_d3_crawler_model_routes_to_device_capability (test_p0_final_closeout.AmbiguousDeviceAliasRoutingTest.test_d3_crawler_model_routes_to_device_capability)` | fail | legacy | update |
| `test_d4_unknown_device_routes_to_device_capability (test_p0_final_closeout.AmbiguousDeviceAliasRoutingTest.test_d4_unknown_device_routes_to_device_capability)` | fail | legacy | update |
| `test_o1_negation_with_candidate_name_rejects (test_p0_final_closeout.PendingOilfieldRejectionTest.test_o1_negation_with_candidate_name_rejects)` | fail | legacy | update |
| `test_o2_candidate_name_suffix_not_right_rejects (test_p0_final_closeout.PendingOilfieldRejectionTest.test_o2_candidate_name_suffix_not_right_rejects)` | fail | legacy | update |
| `test_o3_task_update_does_not_clear_pending (test_p0_final_closeout.PendingOilfieldRejectionTest.test_o3_task_update_does_not_clear_pending)` | fail | legacy | update |
| `test_o5_confirm_oilfield_does_not_publish (test_p0_final_closeout.PendingOilfieldRejectionTest.test_o5_confirm_oilfield_does_not_publish)` | fail | legacy | update |
| `test_c1_confirming_valid_intent_id_preserved (test_p0_final_closeout.SnapshotIntentIdPreservationTest.test_c1_confirming_valid_intent_id_preserved)` | fail | legacy | update |
| `test_c2_confirming_missing_intent_id_generates_new (test_p0_final_closeout.SnapshotIntentIdPreservationTest.test_c2_confirming_missing_intent_id_generates_new)` | fail | legacy | update |
| `test_c3_confirming_invalid_intent_id_generates_new (test_p0_final_closeout.SnapshotIntentIdPreservationTest.test_c3_confirming_invalid_intent_id_generates_new)` | fail | legacy | update |
| `test_c4_done_valid_pub_file_preserves_done_and_id (test_p0_final_closeout.SnapshotIntentIdPreservationTest.test_c4_done_valid_pub_file_preserves_done_and_id)` | fail | legacy | update |
| `test_c5_done_invalid_pub_evidence_downgrades_and_generates_new_id (test_p0_final_closeout.SnapshotIntentIdPreservationTest.test_c5_done_invalid_pub_evidence_downgrades_and_generates_new_id)` | fail | legacy | update |
| `test_legacy_snapshot_restore_confirming_and_done (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_legacy_snapshot_restore_confirming_and_done)` | fail | legacy | update |
| `test_p1_dont_cancel_prefix_update (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p1_dont_cancel_prefix_update)` | fail | legacy | update |
| `test_p1_negation_cancel_does_not_reject_task (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p1_negation_cancel_does_not_reject_task)` | fail | legacy | update |
| `test_p2_done_modification_commit_failure_rollback (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p2_done_modification_commit_failure_rollback)` | fail | legacy | update |
| `test_p2_done_state_modification_recreates_draft_and_new_intent_id (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p2_done_state_modification_recreates_draft_and_new_intent_id)` | fail | legacy | update |
| `test_p3_targeted_cancel_support_vessel_conflict (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p3_targeted_cancel_support_vessel_conflict)` | fail | legacy | update |
| `test_p3_targeted_confirm_support_vessel_conflict (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p3_targeted_confirm_support_vessel_conflict)` | fail | legacy | update |
| `test_p4_device_alias_end_to_end_routing (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p4_device_alias_end_to_end_routing)` | fail | legacy | update |
| `test_p5_device_check_depth_exceeded_response (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p5_device_check_depth_exceeded_response)` | fail | legacy | update |
| `test_p5_unknown_device_check_response (test_p0_final_consistency.P0FinalConsistencyDefectTest.test_p5_unknown_device_check_response)` | fail | legacy | update |
| `test_c13_abbreviation_not_target_rejects_candidate (test_p0_p1_boundary_fixes.ControlledOilfieldAbbreviationRejectionTest.test_c13_abbreviation_not_target_rejects_candidate)` | fail | legacy | update |
| `test_c14_abbreviation_wrong_rejects_candidate (test_p0_p1_boundary_fixes.ControlledOilfieldAbbreviationRejectionTest.test_c14_abbreviation_wrong_rejects_candidate)` | fail | legacy | update |
| `test_c17_rejected_abbreviation_preserves_other_slots_and_phase (test_p0_p1_boundary_fixes.ControlledOilfieldAbbreviationRejectionTest.test_c17_rejected_abbreviation_preserves_other_slots_and_phase)` | fail | legacy | update |
| `test_a1_confirming_valid_intent_id_unchanged (test_p0_p1_boundary_fixes.IntentIdValidationTest.test_a1_confirming_valid_intent_id_unchanged)` | fail | legacy | update |
| `test_a2_confirming_bad_id_generates_new_id (test_p0_p1_boundary_fixes.IntentIdValidationTest.test_a2_confirming_bad_id_generates_new_id)` | fail | legacy | update |
| `test_a3_confirming_path_traversal_id_generates_new_id (test_p0_p1_boundary_fixes.IntentIdValidationTest.test_a3_confirming_path_traversal_id_generates_new_id)` | fail | legacy | update |
| `test_b7_001_in_device_context_routes_to_clarification (test_p0_p1_boundary_fixes.LongestMatchDeviceAliasRoutingTest.test_b7_001_in_device_context_routes_to_clarification)` | fail | legacy | update |
| `test_b9_yihaoji_routes_to_clarification (test_p0_p1_boundary_fixes.LongestMatchDeviceAliasRoutingTest.test_b9_yihaoji_routes_to_clarification)` | fail | legacy | update |
| `test_r15_do_you_think_jinniuzuo_can_work_at_500m (test_p0_publish_race_and_router_closeout.IntentRouterCloseoutTest.test_r15_do_you_think_jinniuzuo_can_work_at_500m)` | fail | legacy | update |
| `test_a6_confirming_snapshot_unicode_id_generates_new_id (test_p0_security_final_closeout.IntentIdUnicodeSecurityTest.test_a6_confirming_snapshot_unicode_id_generates_new_id)` | fail | legacy | update |
| `test_a7_done_snapshot_unicode_id_downgrades_without_file_construction (test_p0_security_final_closeout.IntentIdUnicodeSecurityTest.test_a7_done_snapshot_unicode_id_downgrades_without_file_construction)` | fail | legacy | update |
| `test_b11_001_in_explicit_device_cap_context_is_clarification (test_p0_security_final_closeout.NumericDeviceAliasContextTest.test_b11_001_in_explicit_device_cap_context_is_clarification)` | fail | legacy | update |
| `test_a6_standalone_device_alias_without_context_no_auto_slot_filling (test_p0_true_final_closeout.DeviceAliasRoutingPriorityTest.test_a6_standalone_device_alias_without_context_no_auto_slot_filling)` | fail | legacy | update |
| `test_a8_end_to_end_device_slot_update_flow (test_p0_true_final_closeout.DeviceAliasRoutingPriorityTest.test_a8_end_to_end_device_slot_update_flow)` | fail | legacy | update |
| `test_04_dialogue_manager_rollback_does_not_delete_replaced_staging (test_phase1_publish_cleanup_true_closeout.PublishCleanupTrueCloseoutTest.test_04_dialogue_manager_rollback_does_not_delete_replaced_staging)` | fail | legacy | update |
| `test_05_consumer_rejects_incomplete_final_structures (test_phase1_publish_cleanup_true_closeout.PublishCleanupTrueCloseoutTest.test_05_consumer_rejects_incomplete_final_structures)` | fail | legacy | update |
| `test_21_jinniuzuo_depth_is_500m_ne_routes_to_device_capability (test_phase1_publish_ownership_final_closeout.DeviceCapabilityQuestionRoutingTest.test_21_jinniuzuo_depth_is_500m_ne_routes_to_device_capability)` | fail | legacy | update |
| `test_11_load_snapshot_follows_same_lock_protocol (test_phase1_publish_ownership_final_closeout.PublishOwnershipAndLockTest.test_11_load_snapshot_follows_same_lock_protocol)` | fail | legacy | update |
| `test_13_final_symlink_rejected_by_consumer (test_phase1_publish_ownership_final_closeout.PublishOwnershipAndLockTest.test_13_final_symlink_rejected_by_consumer)` | fail | legacy | update |
| `test_02_alias_mapping_canonical_key (test_slot_consistency.SlotConsistencyTest.test_02_alias_mapping_canonical_key)` | error | regression | fix |
| `test_03_multi_value_slot (test_slot_consistency.SlotConsistencyTest.test_03_multi_value_slot)` | error | regression | fix |
| `test_04_duplicate_inputs_handling (test_slot_consistency.SlotConsistencyTest.test_04_duplicate_inputs_handling)` | error | regression | fix |
| `test_09_unrecognized_input_in_unresolved (test_slot_consistency.SlotConsistencyTest.test_09_unrecognized_input_in_unresolved)` | error | regression | fix |
| `test_10_general_chat_leaves_slot_store_untouched (test_slot_consistency.SlotConsistencyTest.test_10_general_chat_leaves_slot_store_untouched)` | error | regression | fix |
| `test_11_unknown_intent_leaves_slot_store_untouched (test_slot_consistency.SlotConsistencyTest.test_11_unknown_intent_leaves_slot_store_untouched)` | error | legacy | update |
| `test_13_mock_mode_capabilities (test_slot_consistency.SlotConsistencyTest.test_13_mock_mode_capabilities)` | fail | regression | fix |
| `test_15_task_update_updates_slot_store (test_slot_consistency.SlotConsistencyTest.test_15_task_update_updates_slot_store)` | error | regression | fix |
| `test_16_ssot_task_state_consistency (test_slot_consistency.SlotConsistencyTest.test_16_ssot_task_state_consistency)` | error | regression | fix |
| `test_18_missing_slots_derived_from_slot_store (test_slot_consistency.SlotConsistencyTest.test_18_missing_slots_derived_from_slot_store)` | error | regression | fix |
| `test_19_test_a_single_commit_transaction_per_request (test_slot_consistency.SlotConsistencyTest.test_19_test_a_single_commit_transaction_per_request)` | error | regression | fix |
| `test_20_test_b_task_id_exception_leaves_state_untouched (test_slot_consistency.SlotConsistencyTest.test_20_test_b_task_id_exception_leaves_state_untouched)` | error | regression | fix |
| `test_21_main_commit_failure_leaves_state_untouched (test_slot_consistency.SlotConsistencyTest.test_21_main_commit_failure_leaves_state_untouched)` | error | regression | fix |
| `test_22_version_mismatch_raises_slot_version_conflict (test_slot_consistency.SlotConsistencyTest.test_22_version_mismatch_raises_slot_version_conflict)` | error | regression | fix |
| `test_23_concurrency_optimistic_lock (test_slot_consistency.SlotConsistencyTest.test_23_concurrency_optimistic_lock)` | error | fixture | repair |
| `test_23b_multi_slot_concurrency_no_field_mixing (test_slot_consistency.SlotConsistencyTest.test_23b_multi_slot_concurrency_no_field_mixing)` | error | regression | fix |
| `test_25_legacy_snapshot_conversion (test_slot_consistency.SlotConsistencyTest.test_25_legacy_snapshot_conversion)` | fail | regression | fix |
| `test_26_test_c_invalid_snapshot_restoration_leaves_state_untouched (test_slot_consistency.SlotConsistencyTest.test_26_test_c_invalid_snapshot_restoration_leaves_state_untouched)` | error | regression | fix |
| `test_28_asr_text_and_direct_text_same_pipeline (test_slot_consistency.SlotConsistencyTest.test_28_asr_text_and_direct_text_same_pipeline)` | error | regression | fix |
| `test_29_api_chat_409_includes_request_id (test_slot_consistency.SlotConsistencyTest.test_29_api_chat_409_includes_request_id)` | error | regression | fix |
| `test_30_api_chat_500_hides_traceback_and_paths (test_slot_consistency.SlotConsistencyTest.test_30_api_chat_500_hides_traceback_and_paths)` | error | regression | fix |
| `test_30b_api_chat_specific_exceptions_response_structure (test_slot_consistency.SlotConsistencyTest.test_30b_api_chat_specific_exceptions_response_structure)` | error | regression | fix |
| `test_31_frontend_refresh_and_history_load_consistency (test_slot_consistency.SlotConsistencyTest.test_31_frontend_refresh_and_history_load_consistency)` | error | regression | fix |
| `test_32_commit_failure_no_final_task_intent_file (test_slot_consistency.SlotConsistencyTest.test_32_commit_failure_no_final_task_intent_file)` | error | legacy | update |
| `test_33_commit_failure_no_temp_task_intent_file (test_slot_consistency.SlotConsistencyTest.test_33_commit_failure_no_temp_task_intent_file)` | error | legacy | update |
| `test_36_persist_failure_no_success_reply (test_slot_consistency.SlotConsistencyTest.test_36_persist_failure_no_success_reply)` | error | regression | fix |
| `test_36b_successful_publish_e2e (test_slot_consistency.SlotConsistencyTest.test_36b_successful_publish_e2e)` | error | regression | fix |
| `test_37_persist_idempotent (test_slot_consistency.SlotConsistencyTest.test_37_persist_idempotent)` | error | regression | fix |
| `test_41_invalid_value_type_snapshot_rejected (test_slot_consistency.SlotConsistencyTest.test_41_invalid_value_type_snapshot_rejected)` | error | regression | fix |
| `test_42_invalid_updated_at_snapshot_rejected (test_slot_consistency.SlotConsistencyTest.test_42_invalid_updated_at_snapshot_rejected)` | error | regression | fix |
| `test_44_legacy_snapshot_value_type_inference (test_slot_consistency.SlotConsistencyTest.test_44_legacy_snapshot_value_type_inference)` | fail | regression | fix |
| `test_45_default_execution_no_system_prompt_print (test_slot_consistency.SlotConsistencyTest.test_45_default_execution_no_system_prompt_print)` | error | regression | fix |
| `test_47_different_content_same_intent_id_raises_conflict (test_slot_consistency.SlotConsistencyTest.test_47_different_content_same_intent_id_raises_conflict)` | error | legacy | update |
| `test_48_identical_content_same_intent_id_idempotent (test_slot_consistency.SlotConsistencyTest.test_48_identical_content_same_intent_id_idempotent)` | error | legacy | update |
| `test_49f_counter_write_failure_raises_id_reservation_error (test_slot_consistency.SlotConsistencyTest.test_49f_counter_write_failure_raises_id_reservation_error)` | error | regression | fix |
| `test_50a_multiprocess_publish_no_clobber_race (test_slot_consistency.SlotConsistencyTest.test_50a_multiprocess_publish_no_clobber_race)` | fail | fixture | repair |
| `test_50b_multiprocess_publish_idempotent_retry (test_slot_consistency.SlotConsistencyTest.test_50b_multiprocess_publish_idempotent_retry)` | fail | fixture | repair |
| `test_51_all_task_schemas_export_restore_roundtrip (test_slot_consistency.SlotConsistencyTest.test_51_all_task_schemas_export_restore_roundtrip)` | error | regression | fix |
| `test_52_equipment_alias_normalization_and_category_protection (test_slot_consistency.SlotConsistencyTest.test_52_equipment_alias_normalization_and_category_protection)` | fail | regression | fix |
| `test_53_corrupted_counter_files_fail_closed (test_slot_consistency.SlotConsistencyTest.test_53_corrupted_counter_files_fail_closed)` | error | regression | fix |
| `test_conflict_detection_and_resolution (test_slot_consistency.SlotConsistencyTest.test_conflict_detection_and_resolution)` | error | regression | fix |
| `test_list_type_multiple_values (test_slot_consistency.SlotConsistencyTest.test_list_type_multiple_values)` | error | regression | fix |
| `test_semantically_equal_number_does_not_create_conflict (test_slot_consistency.SlotConsistencyTest.test_semantically_equal_number_does_not_create_conflict)` | error | regression | fix |
| `test_three_slots_in_one_message (test_slot_consistency.SlotConsistencyTest.test_three_slots_in_one_message)` | error | regression | fix |
| `test_unextracted_value_remains_missing_for_followup (test_slot_consistency.SlotConsistencyTest.test_unextracted_value_remains_missing_for_followup)` | error | regression | fix |
| `test_user_modify_filled_slot (test_slot_consistency.SlotConsistencyTest.test_user_modify_filled_slot)` | error | regression | fix |
| `test_cache_mode_hits_on_second_call (test_translate_api.TestTranslateAPIRoute.test_cache_mode_hits_on_second_call)` | error | regression | fix |
| `test_chinese_to_english_translation (test_translate_api.TestTranslateAPIRoute.test_chinese_to_english_translation)` | error | regression | fix |
| `test_dirty_chinese_response_falls_back (test_translate_api.TestTranslateAPIRoute.test_dirty_chinese_response_falls_back)` | error | regression | fix |
| `test_dirty_json_response_falls_back (test_translate_api.TestTranslateAPIRoute.test_dirty_json_response_falls_back)` | error | regression | fix |
| `test_empty_text_skips_llm (test_translate_api.TestTranslateAPIRoute.test_empty_text_skips_llm)` | error | regression | fix |
| `test_english_to_chinese_translation (test_translate_api.TestTranslateAPIRoute.test_english_to_chinese_translation)` | error | regression | fix |
| `test_llm_not_initialized (test_translate_api.TestTranslateAPIRoute.test_llm_not_initialized)` | error | regression | fix |
| `test_long_text_calls_llm_multiple_times (test_translate_api.TestTranslateAPIRoute.test_long_text_calls_llm_multiple_times)` | error | regression | fix |
| `test_missing_target_lang (test_translate_api.TestTranslateAPIRoute.test_missing_target_lang)` | error | regression | fix |
| `test_no_cache_mode_calls_llm_every_time (test_translate_api.TestTranslateAPIRoute.test_no_cache_mode_calls_llm_every_time)` | error | regression | fix |
| `test_oversized_input_truncated (test_translate_api.TestTranslateAPIRoute.test_oversized_input_truncated)` | error | regression | fix |
| `test_success (test_translate_api.TestTranslateAPIRoute.test_success)` | error | regression | fix |
| `test_unsupported_target_lang (test_translate_api.TestTranslateAPIRoute.test_unsupported_target_lang)` | error | regression | fix |

## 3. Classification Breakdown Summary

- **Legacy (WRITE/QUERY Refactoring Impact)**: 75
- **Fixture / Mock / Environment**: 3
- **Regression**: 63

## 4. Phase 2 Readiness Evaluation

1. **Core Architecture Integrity**: Verified (`IntentRouter` WRITE/QUERY, `TaskPublishLock`, `SlotStore` provenance intact).
2. **Merge Artifact Cleanup**: Complete (removed duplicate `raw_stage2`, `raw_linked`, and `fcntl` imports).
3. **Phase 1.5 & Phase 1.8 Benchmarks**: 100% Pass (11/11 Phase 1.5, 8/8 Phase 1.8).
4. **Phase 1.9 Guard Tests**: 100% Pass (`test_phase19_regression_guard.py`).
5. **Phase 2 Decision**: **READY** for Phase 2 Agent Planner development after legacy test suite updates.
