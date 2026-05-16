"""TSS financial model package.

Public API:
    load_inputs(path) -> Inputs
    save_inputs(inputs, path)
    run_model(inputs) -> dict[scenario, ScenarioResult]
"""
from model.config import Inputs, load_inputs, save_inputs  # noqa: F401
from model.runner import run_model, ScenarioResult         # noqa: F401

__all__ = ["Inputs", "load_inputs", "save_inputs", "run_model", "ScenarioResult"]
