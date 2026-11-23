"""Base model settings shared by every config, route, domain and secrets model."""

from pydantic import BaseModel, ConfigDict

MODEL_CONFIG = ConfigDict(
    # A typo in a hand-edited file must fail, not silently drop a setting.
    extra="forbid",
    # YAML already has types: "80" is not a port and "yes" is not a boolean.
    strict=True,
    # Assigning `route.name = "a\nb"` would skip the validators that protect the generator.
    frozen=True,
    # This reduces what `str(error)` shows, but it is not protection. It applies only when this
    # model is the outermost validator, and keys still appear in error locations. Use `safe_errors`.
    hide_input_in_errors=True,
)


class StrictModel(BaseModel):
    """Base for all file models: unknown keys and wrong types fail, and instances are frozen."""

    model_config = MODEL_CONFIG
