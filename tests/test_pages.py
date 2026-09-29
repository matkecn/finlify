"""Tests for the HTML page, static assets, and API documentation routes."""

from __future__ import annotations

import pytest


def test_dashboard_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Finlify" in response.text


@pytest.mark.parametrize(
    "path",
    ["/static/app.js", "/static/charts.js", "/static/style.css", "/static/favicon.svg"],
)
def test_static_assets_are_served(client, path):
    assert client.get(path).status_code == 200


def test_legacy_favicon_path_returns_svg(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_demo_endpoint_is_gone(client):
    assert client.get("/api/demo-data").status_code == 404


def test_openapi_docs_are_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Finlify"


def test_unknown_route_returns_404(client):
    assert client.get("/api/does-not-exist").status_code == 404
