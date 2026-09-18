"""稳定的服务身份信息。"""

SERVICE_ID = "food-testing"
SERVICE_NAME = "糕点抽检实验室协作"


def health_payload() -> dict[str, str]:
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}
