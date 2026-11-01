import pytest

from dashulya.database import Database
from dashulya.memory.lexical import LexicalIndex


@pytest.mark.usefixtures("_setup")
class TestLexicalIndex:
    @pytest.fixture
    def _setup(self, database: Database) -> None:
        self.database = database

    async def test_long_tokens_remain_searchable_beyond_postgres_index_tuple_limit(
        self,
    ) -> None:
        index = LexicalIndex(self.database)
        texts = ["prefix" + "x" * 12000, "prefix prefix", "unrelated"]
        await index.prepare(texts)
        keys = [index.key(text) for text in texts]
        assert await index.search(keys, keys, ["prefix"], limit=3) == [keys[1], keys[0]]

    async def test_bm25_uses_corpus_statistics_before_eligible_filter_and_limit(
        self,
    ) -> None:
        index = LexicalIndex(self.database)
        texts = [
            "redis",
            "redirection",
            "redis redis postgres",
            "postgres",
            "other words here",
        ]
        await index.prepare(texts)
        # Cached documents outside the authorized corpus must not change df or
        # average document length. These would reverse the two-term ranking.
        await index.prepare([f"postgres private {i}" for i in range(40)])
        keys = [index.key(text) for text in texts]
        assert await index.search(keys, keys, ["red"], limit=5) == [
            keys[0],
            keys[1],
            keys[2],
        ]
        assert await index.search(keys, keys, ["red", "red"], limit=5) == [
            keys[0],
            keys[1],
            keys[2],
        ]
        assert await index.search(keys, keys, ["postgres"], limit=5) == [keys[3], keys[2]]
        assert await index.search(keys, keys, ["red", "postgres"], limit=5) == [
            keys[3],
            keys[2],
            keys[0],
            keys[1],
        ]
        # Recomputing statistics over just these two eligible documents would
        # incorrectly tie them and put redis first by its corpus position.
        assert await index.search(
            keys, [keys[0], keys[3]], ["red", "postgres"], limit=1
        ) == [keys[3]]
        assert await index.search(keys, [keys[2], keys[1]], ["red"], limit=1) == [keys[1]]

    async def test_unicode_prefixes_respect_folding_and_scalar_boundaries(
        self, subtests: pytest.Subtests
    ) -> None:
        index = LexicalIndex(self.database)
        texts = [
            "naïve CAFÉ cafe\u0301 Straße STRASSE",
            "инструменты инструментами",
            "project🤯 tools🤷 привет\u0301 ΙΣΟΣ ὈΔΥΣΣΕΎΣ",
            "\u0301start separate 👩\u200d💻 combining\u036f",
            "edge\ud7ff tail \U0010ffff maximal \U0010ffffmore",
            "edge\ue000 next scalar \U0010fffftail",
            "carry\U0010ffffvalue",
            "carrz other",
        ]
        await index.prepare(texts)
        keys = [index.key(text) for text in texts]
        for prefix, positions in [
            ("café", [0]),
            ("naïve", [0]),
            ("strasse", [0]),
            ("инструмент", [1]),
            ("project", [2]),
            ("привет", [2]),
            ("edge\ud7ff", [4]),
            ("\U0010ffff", [4, 5]),
            ("carry\U0010ffff", [6]),
        ]:
            with subtests.test(prefix=prefix):
                assert await index.search(keys, keys, [prefix], limit=8) == [
                    keys[i] for i in positions
                ]
