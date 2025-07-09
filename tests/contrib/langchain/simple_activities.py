from typing import Any, Dict
from temporalio import activity


# Activity for testing activity-as-tool conversion
@activity.defn
async def simple_test_activity(message: str) -> Dict[str, Any]:
    """Simple test activity for testing activity-as-tool conversion."""
    return {"message": message, "processed": True, "result": f"Processed: {message}"}


# Simple test activities for activity-as-tool conversion
@activity.defn(name="weather_check")
async def simple_weather_check(city: str) -> Dict[str, Any]:
    """Simple weather check activity for testing."""
    return {
        "city": city,
        "temperature": "25°C",
        "condition": "Sunny",
        "humidity": "60%",
    }


@activity.defn(name="calculation")
async def simple_calculation(x: float, y: float) -> Dict[str, Any]:
    """Simple calculation activity for testing."""
    return {
        "input_x": x,
        "input_y": y,
        "sum": x + y,
        "product": x * y,
        "operation": "basic_math",
    }


@activity.defn(name="uppercase_activity")
async def uppercase_activity(text: str) -> str:
    """Simple uppercase activity for testing."""
    return text.upper()


@activity.defn(name="greet_activity")
async def greet_activity(whom: str) -> str:
    """Simple greet activity for testing."""
    return f"Hello, {whom}!"


@activity.defn(name="word_count_activity")
async def word_count_activity(text: str) -> Dict[str, Any]:
    return {"word_count": len(text.split())}


@activity.defn(name="char_count_activity")
async def char_count_activity(text: str) -> Dict[str, Any]:
    return {"char_count": len(text)}


@activity.defn(name="unique_word_count_activity")
async def unique_word_count_activity(text: str) -> Dict[str, Any]:
    return {"unique_words": len(set(text.split()))}
