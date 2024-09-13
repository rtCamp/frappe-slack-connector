import json

from werkzeug.wrappers import Response


def send_http_response(
    message: str | None = None,
    *,
    body: dict | None = None,
    status_code: int = 200,
    success: bool = True,
    data: dict | None = None,
    is_empty: bool = False,
) -> None:
    """
    Send an HTTP response with the given status code and data
    If `is_empty` is True, the response will be empty
    If `body` is provided, it will be used as the raw response
    Otherwise, return a formatted JSON response
    """
    response = None
    if body:
        response = json.dumps(body)
    if is_empty:
        response = None
    else:
        response = json.dumps(
            {
                "success": success and status_code >= 200 and status_code < 400,
                "message": message,
                "data": data or {},
            }
        )

    return Response(
        response=response,
        status=status_code,
        content_type="application/json",
    )
