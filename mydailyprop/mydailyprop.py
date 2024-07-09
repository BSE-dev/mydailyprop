"""Welcome to Reflex! This file outlines the steps to create a basic app."""

from operator import add
import os
from typing import Annotated, Any, AsyncGenerator, Dict, List
from typing_extensions import TypedDict
from langchain_core.pydantic_v1 import BaseModel, Field

from langgraph.graph import StateGraph
from langchain_openai import ChatOpenAI
from langchain import hub
from langchain_community.document_loaders import FireCrawlLoader
import reflex as rx  # type: ignore
# Reflex does not provide type hints at the moment

model = ChatOpenAI(model='gpt-3.5-turbo', temperature=0.7, streaming=True)
# model = ChatOpenAI(model='gpt-4o', temperature=0.7, streaming=True)
# TODO use gpt-4 for get_contents (better results)


class Editorial(BaseModel):
    title: str = Field(..., title="Editorial title")
    date: str = Field(..., title="Editorial publication date")
    lede: str = Field(..., title="Editorial lede")
    body: str = Field(
        ...,
        title="Article body. Preserve the original paragraphs and sub-titles \
            structure.")

    def markdown(self) -> str:
        return f"# {self.title}\n\n**{self.lede}**\n\n{self.body}"


class GraphState(TypedDict):

    url:                    Annotated[str, add]
    editorial:              Annotated[Editorial, add]
    news_critique:          Annotated[str, add]
    psychological_analysis: Annotated[str, add]
    propaganda_synthesis:   Annotated[str, add]


# 'get_contents' Node
_get_contents_prompt = hub.pull("mydailyprop-get_contents")
_get_contents_chain = _get_contents_prompt | model.with_structured_output(
    Editorial)


def _get_contents(state: GraphState) -> dict[str, Any]:
    loader = FireCrawlLoader(
        api_key=os.getenv('FIRECRAWL_API_KEY'),
        url=state["url"],
        mode="scrape"
    )
    docs = loader.load()
    extracted_json_data = docs[0].page_content

    editorial = _get_contents_chain.invoke({
        "json_data": extracted_json_data, }
    )

    return {
        "editorial": editorial,
    }


# 'critique' Node
_critique_prompt = hub.pull("mydailyprop-critique")
_critique_chain = _critique_prompt | model


def _critique(state: GraphState) -> dict[str, Any]:
    critique = _critique_chain.invoke({
        "editorial":    state["editorial"].markdown(),
        "date":         state["editorial"].date}
    )
    return {
        "news_critique": critique.content
    }


# 'psychological' Node
_psychological_prompt = hub.pull("mydailyprop-psychological")
_psychological_chain = _psychological_prompt | model


def _psychological(state: GraphState) -> dict[str, Any]:
    psychological = _psychological_chain.invoke({
        "editorial":    state["editorial"].markdown(),
        "date":         state["editorial"].date, }
    )
    return {
        "psychological_analysis": psychological.content
    }


# 'synthesis' Node
_synthesis_prompt = hub.pull("mydailyprop-synthesis")
_synthesis_chain = _synthesis_prompt | model


def _synthesis(state: GraphState) -> dict[str, Any]:
    synthesis = _synthesis_chain.invoke({
        "editorial":        state["editorial"].markdown(),
        "date":             state["editorial"].date,
        "critique":         state["news_critique"],
        "psychological":    state["psychological_analysis"]}
    )
    return {
        "propaganda_synthesis": synthesis.content
    }


graph_builder = StateGraph(GraphState)

graph_builder.add_node("get_contents", _get_contents)
graph_builder.set_entry_point("get_contents")

graph_builder.add_node("critique", _critique)
graph_builder.add_edge("get_contents", "critique")

graph_builder.add_node("psychological", _psychological)
graph_builder.add_edge("get_contents", "psychological")

graph_builder.add_node("synthesis", _synthesis)
graph_builder.add_edge("critique", "synthesis")
graph_builder.add_edge("psychological", "synthesis")

graph_builder.set_finish_point("synthesis")
graph = graph_builder.compile()


class DecryptState(rx.State):  # type: ignore
    """The app state."""

    url: str = ""
    is_running: bool = False
    cards: Dict[str, Dict[str, str]] = {}

    def upsert_card(
        self,
        card_name: str,
        card_desc: str,
        card_content: str,
        color_scheme: str
    ) -> None:
        """Insert or update a card at the beginning of the cards dictionary."""
        if card_name in self.cards:
            self.cards[card_name]["content"] += card_content
        else:
            new_dict_entry = {card_name: {
                "desc": card_desc,
                "content": card_content,
                "color_scheme": color_scheme}}
            self.cards = {**new_dict_entry, **self.cards}

    async def decrypt(self) -> AsyncGenerator[None, None]:

        self.is_running = True
        # reset the cards
        self.cards = {}
        yield

        async for event in graph.astream_events(
            {"url":  self.url},
            version="v2"
        ):
            kind = event["event"]
            # emitted for each streamed token
            if kind == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                # only print non-empty content (not tool calls)
                if content:
                    origin_node = event["metadata"]["langgraph_node"]
                    if origin_node == "critique":
                        self.upsert_card(
                            card_name="critique",
                            card_desc="Critique journalistique (générée)",
                            card_content=content,
                            color_scheme="indigo")
                    elif origin_node == "psychological":
                        self.upsert_card(
                            card_name="psychological",
                            card_desc="Analyse psychologique (générée)",
                            card_content=content,
                            color_scheme="mint")
                    elif origin_node == "synthesis":
                        self.upsert_card(
                            card_name="synthesis",
                            card_desc="Synthèse propagande (générée)",
                            card_content=content,
                            color_scheme="crimson")
                    yield
            # emitted when a model call finishes (we want to catch the
            # Editorial's contents extraction as JSON using
            # with_structured_output (using 'functions' / 'tools')
            if kind == "on_chat_model_end":
                # using 'functions' / 'tools' creates an event with tool_calls
                if event["data"]["output"].tool_calls:
                    edito_dict = event["data"]['output'].tool_calls[0]['args']
                    self.upsert_card(
                        card_name="editorial",
                        card_desc="Contenu de l'édito (récupéré)",
                        card_content=Editorial(**edito_dict).markdown(),
                        color_scheme="blue")
                    yield

        self.is_running = False


def action_bar() -> rx.Component:
    return rx.hstack(
        rx.input(
            placeholder="Enter URL here...",
            on_change=DecryptState.set_url,
            flex_grow="1",
        ),
        rx.button(
            "decrypt",
            on_click=DecryptState.decrypt,
            loading=DecryptState.is_running,
        ),
        width="80%",
    )


def content_card(card_contents: List[Any]) -> rx.Component:
    return rx.card(
        rx.heading(
            card_contents[1]["desc"],
            color_scheme=card_contents[1]["color_scheme"],
            size="3"),
        rx.text(
            rx.markdown(card_contents[1]["content"]),
            color_scheme=card_contents[1]["color_scheme"],),
        max_height="200px",
        overflow="auto",
    )


def index() -> rx.Component:
    return rx.container(
        rx.color_mode.button(position="top-right"),
        rx.vstack(
            rx.heading("My Daily Propaganda", size="9"),
            rx.text(
                "decrypted ",
                rx.code("@ Brest Social Engines"),
                size="5",
            ),
            action_bar(),
            rx.foreach(
                DecryptState.cards,
                content_card,
            ),
            spacing="5",
            justify="center",
            align="center",
            min_height="85vh",
        ),
        rx.logo(),
    )


app = rx.App()
app.add_page(index, title="My Daily Propaganda")
