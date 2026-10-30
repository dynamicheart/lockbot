"""Public category hints never turn unrecognized free text into public metadata."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

TASK_CATEGORIES = {
    "operator_development": "算子开发",
    "operator_testing": "算子测试",
    "communication_testing": "通信库测试",
    "unit_testing": "单元测试",
    "functional_testing": "功能测试",
    "performance_testing": "性能测试",
    "model_development": "模型开发",
    "model_load_testing": "模型压测",
    "code_review": "代码审查",
    "debugging": "故障排查",
    "other": "其他任务",
}


def validate_task_category(value):
    return value if isinstance(value, str) and value in TASK_CATEGORIES else None


class TaskCategoryNotice(BaseModel):
    code: Literal["optional_public_task_category"] = "optional_public_task_category"
    reason: Literal["missing", "unsupported_category"]
    context_url: str = "/api/agent/context"
    allowed_categories: dict[str, str]
    optional: bool = True
    next_request_only: bool = True
    do_not_replay_command: bool = True
    guidance: str


class TaskCategoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_category: str | None = Field(
        default=None,
        max_length=48,
        pattern=r"^[^\r\n\x00-\x1f\x7f]*$",
        description="Optional public preset ID. Unrecognized strings are ignored; see /api/agent/context.",
    )
    _task_category_notice_reason: str | None = PrivateAttr(default=None)

    @field_validator("task_category", mode="before")
    @classmethod
    def category_format(cls, value):
        if isinstance(value, str) and (len(value) > 48 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("task_category must be at most 48 characters of plain single-line text")
        return value

    _category = field_validator("task_category")(validate_task_category)

    @model_validator(mode="wrap")
    @classmethod
    def record_category_notice(cls, data, handler):
        # Run all type/length/extra-field validation before marking an unknown string ignored.
        result = handler(data)
        if "task_category" not in result.model_fields_set:
            result._task_category_notice_reason = "missing"
        elif isinstance(data, dict) and isinstance(data.get("task_category"), str) and result.task_category is None:
            result._task_category_notice_reason = "unsupported_category"
        return result

    def category_notice(self):
        if self._task_category_notice_reason is None:
            return None
        reason = self._task_category_notice_reason
        prefix = (
            "This request omitted task_category. Your resource may retain its existing valid category. "
            if reason == "missing"
            else "The supplied public category was not recognized and was ignored; it is not stored or published. "
        )
        return TaskCategoryNotice(
            reason=reason,
            allowed_categories=TASK_CATEGORIES,
            guidance=prefix
            + (
                "Other valid input is processed normally; this notice is not an error or proof of ownership. "
                "Check reply and resource state. For future requests choose one allowed task_category ID, "
                "or send null to leave it undisclosed. Do not infer or publish detailed Agent task notes, "
                "and do not replay this command to add a category. "
                "Updating task metadata does not acquire or renew resources."
            ),
        )
