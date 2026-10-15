"""機能をまたいで受け渡すデータの型。"""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints


class FetchedItem(BaseModel):
    """フェッチャー（004）が取得した 1 記事。既読判定（003）に渡す。"""

    model_config = ConfigDict(frozen=True)

    source_id: str
    url: str
    title: Annotated[str, StringConstraints(min_length=1)]
    published_at: AwareDatetime | None = None
    content: str = ""
