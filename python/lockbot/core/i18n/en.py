"""English (en) message translations for lockbot."""

MESSAGES = {
    # ── Duration formatting ──
    "duration.days": "{value} days",
    "duration.hours": "{value} hours",
    "duration.minutes": "{value} minutes",
    # ── Access mode ──
    "access_mode.shared": "(shared)",
    "access_mode.exclusive": "(exclusive)",
    # ── Status ──
    "status.idle": "idle",
    # ── Success messages ──
    "success.resource_locked": "✅ Resource locked successfully\n\n",
    "success.resource_released": "✅ Resource released successfully\n\n",
    "success.resource_force_released": "✅ Resource force-released by {user_id}\n\n",
    # ── Labels ──
    "label.before_release": "Before release:\n",
    "label.after_release": "After release:\n",
    "label.before_take": "Before take:\n",
    "label.after_take": "After take:\n",
    "label.queue_list": "⌛️ Queue:\n",
    "label.queue_item": "  {index}. {user_id} {duration} est. wait {wait_time}\n",
    # ── Error messages ──
    "error.invalid_command_format": "❌ Invalid command format: {command}",
    "error.invalid_node_key": "Invalid node '{node_key}'\n\nPlease choose from: {valid_keys}\n",
    "error.duration_must_be_positive": "Duration must be greater than 0\n",
    "error.node_in_use_or_shared": "Node is in use by others or in shared mode\n\n",
    "error.node_exclusive_mode": "Node is in exclusive mode\n\n",
    "error.lock_max_duration_exceeded": "Note: consecutive lock cannot exceed {max_duration}\n\n",
    "error.slock_max_duration_exceeded": "Note: consecutive slock cannot exceed {max_duration}\n\n",
    "error.node_not_requested": "You have not requested this node\n",
    "error.unrecognized_command": "❌ Unrecognized command: {command}",
    "error.unknown_error": "❌ Unknown error: {command}",
    # ── Device-specific errors ──
    "error.device_in_use_or_shared": "Device is in exclusive use or in shared mode\n\n",
    "error.device_exclusive_mode": "Device is in exclusive use\n\n",
    "error.dev_id_range_invalid": "Invalid dev_id\n\nmin<=dev_id<=max, but min({dev_min}) > max({dev_max})\n",
    "error.dev_id_out_of_range": "Invalid dev_id\n\n{node_key} requires 0<=dev_id<{num_devs}\n",
    "error.device_not_requested": "You have not requested this device\n",
    # ── Queue-specific errors ──
    "error.node_in_use_or_not_your_turn": "Node is in use by others, or it is not your turn yet",
    "error.already_locked": "You are already using or have already booked",
    "error.not_in_booking_list": "You are not in the booking queue\n",
    "error.locked_user_cannot_take": "You are already using this node",
    "error.slock_not_supported": "QueueBot does not support slock",
    # ── Queue-specific success ──
    "success.booking_added": "🗓️ Booking added successfully\n\n",
    "success.take_success": "✅ Take successful\n\n",
    "success.take_success_by": "🏁 Resource taken by {user_id}\n\n",
    "success.kicklock_cleared": "✅ Lock cleared by {user_id}\n\n",
    # ── Alerts ──
    "alert.early_time_remaining": "❗️ Resource time remaining is less than {time_alert}\n\n",
    "alert.early_extend_reminder": (
        "If you still need the resource, use lock/slock to extend before it is auto-released\n\n"
    ),
    "alert.early_resource_list_header": "Resources about to be released:\n",
    "alert.auto_released_title": "❗️ Resource auto-released\n\n",
    "alert.auto_released_list_header": "Released resources:\n",
    # ── Queue alerts ──
    "alert.booking_expired": "❗️ Booking expired and auto-cancelled\n\n",
    "alert.your_turn": "❗️ It's your turn\n\n",
    "alert.your_turn_reminder": (
        "Please use the lock command within {timeout} to lock the resource, or it will be skipped\n\n"
    ),
    # ── Query ──
    "query.cluster_usage_title": "ℹ️ Cluster Usage Details\n\n",
    # ── Device usage ──
    "device_usage.node_header": "{node_key} usage:\n",
    "device_usage.hetero_warning": (
        "❗️ Note: GPU ordering on {node_key}\n"
        "CUDA_VISIBLE_DEVICES is ordered by compute capability (high to low)\n"
        "nvidia-smi is ordered by PCIe address\n"
        "Please use the correct device!\n\n"
    ),
    # ── Help text (NODE) ──
    "help.title": "📖 Usage Guide\n",
    "help.section1_title": "1. Request resource\n",
    "help.rule1_default_duration": (
        "    Rule 1: Default duration {default_duration}, repeated lock extends time, d(day),h(hour),m(min)\n"
    ),
    "help.rule2_early_notification": "    Rule 2: A reminder will be sent when {time_alert} remaining\n",
    "help.rule2_post_expiry_notification": "    Rule 2: A reminder will be sent after resource expires\n",
    "help.rule3_lock_modes": "    Rule 3: lock for exclusive, slock for shared (multiple users can slock)\n",
    "help.section2_title": "2. Release resource (unlock and free are interchangeable)\n",
    "help.free_all": "    free (release all your resources)\n",
    "help.section3_title": "3. Force release others' resource (will notify affected users)\n",
    "help.section4_title": "4. Help: help or h\n",
    "help.section5_title": "5. Query:\n",
    "help.query_at_bot": "    Just @ the bot\n",
    "help.max_duration_warning": "Note: consecutive lock/slock cannot exceed {max_duration}\n\n",
    "help.bot_version": "Bot version: {version}\n",
    "help.bot_id": "Bot ID: {bot_id}\n",
    "help.bot_owner": "Owner: {owner}\n",
    # ── Help text (NODE) command examples ──
    "help.lock_example": "    lock {node} (lock node {node})\n",
    "help.lock_duration_example": "    lock {node} {duration} (lock node {node} for {duration})\n",
    "help.slock_example": "    slock {node} (shared-lock node {node})\n",
    "help.unlock_example": "    unlock {node} (release node {node})\n",
    "help.unlock_all_example": "    unlock (release all your resources)\n",
    "help.kickout_example": "    kickout {node} (force-release node {node})\n",
    "help.query_node_example": "    query {node} (query node {node})\n",
    # ── Help text (DEVICE) command examples ──
    "help.lock_all_devices_example": "    lock {node} (lock all devices on node)\n",
    "help.lock_device_example": "    lock {node} 0 (lock device 0 on {node})\n",
    "help.lock_device_duration_example": "    lock {node} 0 2h (lock device 0 on {node} for 2h)\n",
    "help.lock_device_range_example": "    lock {node} 0-3 (lock devices 0-3 on {node})\n",
    "help.slock_device_range_example": "    slock {node} 0-3 (shared-lock devices 0-3 on {node})\n",
    "help.unlock_device_example": "    unlock {node} (release all your devices on node)\n",
    "help.unlock_device_range_example": "    unlock {node} 0-3 (release devices 0-3 on {node})\n",
    "help.free_device_range_example": "    free {node} 0-3 (release devices 0-3 on {node})\n",
    "help.free_node_all_example": "    free {node} (release all your devices on {node})\n",
    "help.kickout_device_example": "    kickout {node} (force-release all devices on node)\n",
    "help.kickout_device_range_example": "    kickout {node} 0-3 (force-release devices 0-3 on {node})\n",
    "help.kickout_device_range2_example": "    kickout {node} 0 (force-release device 0 on {node})\n",
    "help.resource_list_title": "Resource list:\n",
    "help.resource_list_item": "    {node_key}: dev_id 0~{max_dev}\n",
    # ── Help text (QUEUE) extras ──
    "help.section_booking_title": "2. Book\n",
    "help.book_example": "    book {node} (book node {node})\n",
    "help.book_duration_example": "    book {node} {duration} (book node {node} for {duration})\n",
    "help.section_take_title": "3. Take (preempt)\n",
    "help.take_example": "    take {node} (take node {node})\n",
    "help.section_release_title": "4. Release resource (unlock and free are interchangeable)\n",
    "help.section_kickout_title": "5. Force release others' resource (will notify affected users)\n",
    "help.section_cancel_booking_title": "5. Cancel booking\n",
    "help.section_kicklock_title": "7. Force release lock (booking info preserved)\n",
    "help.rule3_lock_exclusive": "    Rule 3: lock for exclusive access\n",
    "help.section_help_title_queue": "6. Help: help or h\n",
    "help.section_query_title_queue": "7. Query:\n",
    # ── Notify messages (queue) ──
    "notify.node_released_your_turn": "📋 Node {node_key} released, it's {user_id}'s turn\n",
    "notify.please_lock_in_time": "Please use lock {node_key} within {timeout}\n",
    "notify.booking_timeout_cancelled": "⏰ {user_id} booking expired, auto-cancelled\n",
    "notify.take_preempt": "⚠️ {user_id} preempted node {node_key}\n",
    "notify.take_notify_all": "📋 Node {node_key} taken by {user_id}, current usage:\n",
    "notify.wait_time_increased": "Note: wait times have been updated\n\n",
    "notify.resource_available_header": "📢 Resource available\nResource is idle, please lock within {timeout}:\n\n",
    "notify.booking_expired_header": "⚠️ The following bookings have expired:\n",
    "notify.pending_bookings_header": "🗓️ Pending bookings:\n",
    # ── Webhook: bot not running ──
    "webhook.bot_not_running": "⚠️ Bot {bot_name} is not running. "  # noqa: E501
    "Please contact the owner @{owner_username} to start it.",
}
