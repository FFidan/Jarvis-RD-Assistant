"""Tests for embed_texts error handling -- malformed responses, HTTP errors, timeouts."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from jarvis_common.llm_client import embed_texts, LiteLLMConfig


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response with the given JSON and status code."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    return resp


@pytest.fixture()
def mock_client():
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture()
def config():
    return LiteLLMConfig(base_url="http://test:4000", api_key="test-key")


async def test_embed_texts_empty_returns_empty(mock_client, config):
    """Passing an empty list should return [] without making any HTTP call."""
    result = await embed_texts(mock_client, [], config=config)
    assert result == []
    mock_client.post.assert_not_called()


async def test_embed_texts_missing_data_key(mock_client, config):
    """Response without a 'data' key should raise RuntimeError."""
    mock_client.post.return_value = _mock_response({"result": "ok"})
    with pytest.raises(RuntimeError, match="Unexpected embedding response format"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_http_500(mock_client, config):
    """HTTP 500 should raise RuntimeError with status info."""
    mock_client.post.return_value = _mock_response({}, status_code=500)
    with pytest.raises(RuntimeError, match="status"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_timeout(mock_client, config):
    """Timeout should raise RuntimeError with timeout message."""
    mock_client.post.side_effect = httpx.TimeoutException("timed out")
    with pytest.raises(RuntimeError, match="timed out"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_connection_error(mock_client, config):
    """Connection error should raise RuntimeError."""
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(RuntimeError, match="failed"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_malformed_items(mock_client, config):
    """Data items without 'embedding' key should raise RuntimeError."""
    mock_client.post.return_value = _mock_response({"data": [{"no_embedding": True}]})
    with pytest.raises(RuntimeError, match="Unexpected embedding response format"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_success(mock_client, config):
    """Normal success case should return embeddings in order."""
    mock_client.post.return_value = _mock_response({
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ]
    })
    result = await embed_texts(mock_client, ["hello", "world"], config=config)
    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]
    assert result[1] == [0.4, 0.5, 0.6]


async def test_embed_texts_reorders_by_index(mock_client, config):
    """Embeddings returned out of order should be sorted by index."""
    mock_client.post.return_value = _mock_response({
        "data": [
            {"index": 2, "embedding": [0.7, 0.8]},
            {"index": 0, "embedding": [0.1, 0.2]},
            {"index": 1, "embedding": [0.4, 0.5]},
        ]
    })
    result = await embed_texts(mock_client, ["a", "b", "c"], config=config)
    assert result == [[0.1, 0.2], [0.4, 0.5], [0.7, 0.8]]


async def test_embed_texts_sends_correct_payload(mock_client, config):
    """embed_texts should POST to /v1/embeddings with the right payload."""
    mock_client.post.return_value = _mock_response({
        "data": [{"index": 0, "embedding": [1.0]}]
    })
    await embed_texts(mock_client, ["hello"], model="embed", config=config)

    mock_client.post.assert_awaited_once_with(
        "http://test:4000/v1/embeddings",
        json={"model": "embed", "input": ["hello"]},
        headers={"Authorization": "Bearer test-key"},
        timeout=60.0,
    )


async def test_embed_texts_http_401(mock_client, config):
    """HTTP 401 should raise RuntimeError with status info."""
    mock_client.post.return_value = _mock_response({}, status_code=401)
    with pytest.raises(RuntimeError, match="status 401"):
        await embed_texts(mock_client, ["test"], config=config)


async def test_embed_texts_data_is_none(mock_client, config):
    """If 'data' key exists but is None, should raise RuntimeError."""
    mock_client.post.return_value = _mock_response({"data": None})
    with pytest.raises((RuntimeError, TypeError)):
        await embed_texts(mock_client, ["test"], config=config)
