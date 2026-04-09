"""Chinese (zh) message translations for lockbot."""

MESSAGES = {
    # ── Duration formatting ──
    "duration.days": "{value} 天",
    "duration.hours": "{value} 小时",
    "duration.minutes": "{value} 分钟",
    # ── Access mode ──
    "access_mode.shared": "(共享)",
    "access_mode.exclusive": "(独占)",
    # ── Status ──
    "status.idle": "空闲",
    # ── Success messages ──
    "success.resource_locked": "✅【资源申请成功】\n\n",
    "success.resource_released": "✅【资源释放成功】\n\n",
    "success.resource_force_released": "✅【资源强制释放成功】by {user_id}\n\n",
    # ── Labels ──
    "label.before_release": "【释放前】：\n",
    "label.after_release": "【释放后】：\n",
    "label.before_take": "【抢占前】：\n",
    "label.after_take": "【抢占后】：\n",
    "label.queue_list": "⌛️ 排队:\n",
    "label.queue_item": "  {index}. {user_id} {duration} 预计等待 {wait_time}\n",
    # ── Error messages ──
    "error.invalid_command_format": "❌【命令格式有误】{command}",
    "error.invalid_node_key": "【节点{node_key}有误】\n\n节点应在{valid_keys}里面选择\n",
    "error.duration_must_be_positive": "【申请资源时间应大于0】\n",
    "error.node_in_use_or_shared": "【节点正在被他人使用或处于共享状态】\n\n",
    "error.node_exclusive_mode": "【节点正处于独占状态】\n\n",
    "error.lock_max_duration_exceeded": "【注意: 目前禁止连续lock超过{max_duration}】\n\n",
    "error.slock_max_duration_exceeded": "【注意: 目前禁止连续slock超过{max_duration}】\n\n",
    "error.node_not_requested": "【你并未申请过该节点资源】\n",
    "error.unrecognized_command": "❌【未识别的命令】{command}",
    "error.unknown_error": "❌【未知错误】{command}",
    # ── Device-specific errors ──
    "error.device_in_use_or_shared": "【设备正在被他人独占使用或处于共享状态】\n\n",
    "error.device_exclusive_mode": "【设备正在被独占使用】\n\n",
    "error.dev_id_range_invalid": "【dev_id有误】\n\nmin<=dev_id<=max, 然而min({dev_min}) > max({dev_max})\n",
    "error.dev_id_out_of_range": "【dev_id有误】\n\n{node_key}应保证0<=dev_id<{num_devs}\n",
    "error.device_not_requested": "【你并未申请过该设备资源】\n",
    # ── Queue-specific errors ──
    "error.node_in_use_or_not_your_turn": "节点正在被他人使用，或未到排队顺序",
    "error.already_locked": "你已经正在使用或者已经排过队",
    "error.not_in_booking_list": "【你不在排队列表中】\n",
    "error.locked_user_cannot_take": "你已经正在使用",
    "error.slock_not_supported": "QueueBot不支持slock",
    # ── Queue-specific success ──
    "success.booking_added": "🗓️【排队成功】\n\n",
    "success.take_success": "✅【抢占成功】\n\n",
    "success.take_success_by": "🏁【资源抢占成功】by {user_id}\n\n",
    "success.kicklock_cleared": "✅【锁定已清空】by {user_id}\n\n",
    # ── Alerts ──
    "alert.early_time_remaining": "❗️【资源可用时间少于{time_alert}】\n\n",
    "alert.early_extend_reminder": "如果还有资源需求, 请及时使用lock/slock命令增加时间, 以免资源自动释放\n\n",
    "alert.early_resource_list_header": "即将被释放的资源:\n",
    "alert.auto_released_title": "❗️【资源自动释放】\n\n",
    "alert.auto_released_list_header": "已释放的资源列表:\n",
    # ── Queue alerts ──
    "alert.booking_expired": "❗️【排队超时自动取消】\n\n",
    "alert.your_turn": "❗️【轮到你了】\n\n",
    "alert.your_turn_reminder": "请在{timeout}内使用lock命令锁定资源, 否则将自动跳过\n\n",
    # ── Query ──
    "query.cluster_usage_title": "ℹ️【集群使用详情】\n\n",
    # ── Device usage ──
    "device_usage.node_header": "{node_key}使用情况:\n",
    "device_usage.hetero_warning": (
        "❗️【注意{node_key}的GPU顺序】\n"
        "CUDA_VISIBLE_DEVICES 按照算力从高到低编号\n"
        "nvidia-smi 按照PCIe地址顺序编号\n"
        "请选择正确的设备进行使用!\n\n"
    ),
    # ── Help text (NODE) ──
    "help.title": "📖【使用方法】\n",
    "help.section1_title": "1. 申请资源\n",
    "help.rule1_default_duration": "    规则1: 默认时间{default_duration}, 重复lock增加时间, d(天),h(时),m(分)\n",
    "help.rule2_early_notification": "    规则2: 当时间剩余{time_alert},会提醒一次\n",
    "help.rule2_post_expiry_notification": "    规则2: 资源时间用时耗尽后,会进行提醒\n",
    "help.rule3_lock_modes": "    规则3: lock申请独占资源, slock申请共享资源(可多人同时slock)\n",
    "help.section2_title": "2. 释放资源 (unlock和free通用)\n",
    "help.free_all": "    free (释放自己申请的所有资源)\n",
    "help.section3_title": "3. 强制释放他人资源 (会at相关人员)\n",
    "help.section4_title": "4. 帮助: help或者h\n",
    "help.section5_title": "5. 查询:\n",
    "help.query_at_bot": "    直接at机器人\n",
    "help.max_duration_warning": "【注意: 目前禁止连续lock/slock超过{max_duration}】\n\n",
    "help.bot_version": "机器人版本: {version}\n",
    "help.bot_id": "机器人ID: {bot_id}\n",
    "help.bot_owner": "管理人: {owner}\n",
    # ── Help text (NODE) command examples ──
    "help.lock_example": "    lock {node} (锁定{node}节点)\n",
    "help.lock_duration_example": "    lock {node} {duration} (锁定{node}节点{duration})\n",
    "help.slock_example": "    slock {node} (共享锁定{node}节点)\n",
    "help.unlock_example": "    unlock {node} (释放{node}节点)\n",
    "help.unlock_all_example": "    unlock (释放所有申请过的资源)\n",
    "help.kickout_example": "    kickout {node} (强制释放{node}节点)\n",
    "help.query_node_example": "    query {node} (查询{node}节点)\n",
    # ── Help text (DEVICE) command examples ──
    "help.lock_all_devices_example": "    lock {node} (锁定当前节点的所有设备)\n",
    "help.lock_device_example": "    lock {node} 0 (锁定{node}节点的0号设备)\n",
    "help.lock_device_duration_example": "    lock {node} 0 2h (锁定{node}节点的0号设备2小时)\n",
    "help.lock_device_range_example": "    lock {node} 0-3 (锁定{node}节点的0-3号设备)\n",
    "help.slock_device_range_example": "    slock {node} 0-3 (共享锁定{node}节点的0-3号设备)\n",
    "help.unlock_device_example": "    unlock {node} (释放当前节点所有申请过的设备)\n",
    "help.unlock_device_range_example": "    unlock {node} 0-3 (释放{node}节点的0-3号设备)\n",
    "help.free_device_range_example": "    free {node} 0-3 (释放{node}节点的0-3号设备)\n",
    "help.free_node_all_example": "    free {node} (释放{node}节点所有申请过的设备)\n",
    "help.kickout_device_example": "    kickout {node} (强制释放当前节点的所有设备)\n",
    "help.kickout_device_range_example": "    kickout {node} 0-3 (强制释放{node}节点的0-3号设备)\n",
    "help.kickout_device_range2_example": "    kickout {node} 0 (强制释放{node}节点的0号设备)\n",
    "help.resource_list_title": "资源列表:\n",
    "help.resource_list_item": "    {node_key}: dev_id 0~{max_dev}\n",
    # ── Help text (QUEUE) extras ──
    "help.section_booking_title": "2. 排队\n",
    "help.book_example": "    book {node} (排队等候{node}节点)\n",
    "help.book_duration_example": "    book {node} {duration} (排队等候{node}节点{duration})\n",
    "help.section_take_title": "3. 抢占\n",
    "help.take_example": "    take {node} (抢占{node}节点)\n",
    "help.section_release_title": "4. 释放资源 (unlock和free通用)\n",
    "help.section_kickout_title": "5. 强制释放他人资源 (会at相关人员)\n",
    "help.section_cancel_booking_title": "5. 取消排队\n",
    "help.section_kicklock_title": "7. 强制释放锁(排队信息不清除)\n",
    "help.rule3_lock_exclusive": "    规则3: lock申请独占资源\n",
    "help.section_help_title_queue": "6. 帮助: help或者h\n",
    "help.section_query_title_queue": "7. 查询:\n",
    # ── Notify messages (queue) ──
    "notify.node_released_your_turn": "📋 节点 {node_key} 已释放，轮到 {user_id} 使用\n",
    "notify.please_lock_in_time": "请在{timeout}内使用 lock {node_key} 命令锁定资源\n",
    "notify.booking_timeout_cancelled": "⏰ {user_id} 排队超时，已自动取消\n",
    "notify.take_preempt": "⚠️ {user_id} 抢占了节点 {node_key}\n",
    "notify.take_notify_all": "📋 节点 {node_key} 已被 {user_id} 抢占，当前使用情况:\n",
    "notify.wait_time_increased": "请注意等待时间已增加 \n\n",
    "notify.resource_available_header": "📢【资源可用提醒】\n资源已空闲，请在 {timeout} 内lock:\n\n",
    "notify.booking_expired_header": "⚠️ 以下预约已失效，请重新预约：\n",
    "notify.pending_bookings_header": "🗓️ 目前待抢锁的预约：\n",
    # ── Webhook: bot not running ──
    "webhook.bot_not_running": "⚠️ 机器人 {bot_name} 尚未启动，请联系管理人 @{owner_username} 启动后再使用。",
}
