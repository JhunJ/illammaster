"""viewer_preview._resolve_entity_pen — ByLayer(256) 이 raw>255 로 오인되지 않는지."""
from app.services.viewer_preview import _resolve_entity_pen, _pen_to_css


def test_bylayer_256_uses_layer_color_not_packed_rgb():
    """256 > 255 이므로 잘못된 분기면 256 그대로 반환 → 회색; 레이어 색(예: 1=빨강)이어야 함."""
    pen = _resolve_entity_pen(
        256,
        "WALL",
        {"WALL": 1},
        {"color_raw": 256, "color_bylayer": True},
    )
    assert pen == 1
    assert _pen_to_css(pen) == "#ff0000"


def test_truecolor_wins_over_bylayer_raw():
    pen = _resolve_entity_pen(
        16744512,
        "0",
        {},
        {"color_raw": 256, "true_color": 16744512, "color_bylayer": True},
    )
    assert pen == 16744512
