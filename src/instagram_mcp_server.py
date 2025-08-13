#!/usr/bin/env python3
"""
Instagram MCP Server - A Model Context Protocol server for Instagram API integration.
"""

import asyncio
import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from types import SimpleNamespace

import structlog
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Prompt, Resource, TextContent, Tool

# Avoid version-specific Notification* imports; use a simple shim instead.

from .config import get_settings
from .instagram_client import InstagramAPIError, InstagramClient
from .models.instagram_models import (
    InsightMetric,
    InsightPeriod,
    MCPToolResult,
    PublishMediaRequest,
)

logger = structlog.get_logger(__name__)
instagram_client: Optional[InstagramClient] = None


class InstagramMCPServer:
    def __init__(self):
        self.settings = get_settings()
        self.server = Server(self.settings.mcp_server_name)
        self._setup_handlers()

    def _setup_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> List[Tool]:
            return [
                Tool(
                    name="get_profile_info",
                    description="Get Instagram business profile info (followers, bio, details).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "account_id": {
                                "type": "string",
                                "description": "Instagram business account ID (optional).",
                            }
                        },
                        "additionalProperties": False,
                    },
                ),
                Tool(
                    name="get_media_posts",
                    description="Get recent media posts with engagement metrics.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "string", "description": "Optional account id"},
                            "limit": {
                                "type": "integer",
                                "description": "Number of posts (max 100)",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 25,
                            },
                            "after": {"type": "string", "description": "Pagination cursor"},
                        },
                        "additionalProperties": False,
                    },
                ),
                Tool(
                    name="get_media_insights",
                    description="Get insights for a specific Instagram post.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "media_id": {
                                "type": "string",
                                "description": "Instagram media ID to fetch insights for",
                            },
                            "metrics": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    # API v22+: 'impressions' removed for media; 'plays' is for reels.
                                    "enum": ["reach", "likes", "comments", "shares", "saves", "plays"],
                                },
                                "description": "Specific metrics (optional).",
                            },
                        },
                        "required": ["media_id"],
                        "additionalProperties": False,
                    },
                ),
                Tool(
                    name="publish_media",
                    description="Upload & publish an image or video with caption/location.",
                    # NOTE: Claude doesn't allow anyOf/oneOf at top level. We validate in code.
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "image_url": {
                                "type": "string",
                                "format": "uri",
                                "description": "Public URL of the image (use either image_url or video_url).",
                            },
                            "video_url": {
                                "type": "string",
                                "format": "uri",
                                "description": "Public URL of the video (use either video_url or image_url).",
                            },
                            "caption": {"type": "string", "description": "Optional caption"},
                            "location_id": {"type": "string", "description": "FB location ID (optional)"},
                        },
                        "additionalProperties": False,
                    },
                ),
                Tool(
                    name="get_account_pages",
                    description="Get Facebook Pages connected to the account and their IG accounts.",
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                ),
                Tool(
                    name="get_account_insights",
                    description="Get account-level insights for the Instagram business account.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "string", "description": "Optional account id"},
                            "metrics": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["reach", "profile_views", "website_clicks"],
                                },
                                "description": "Metrics to retrieve.",
                            },
                            "period": {
                                "type": "string",
                                "enum": ["day", "week", "days_28"],
                                "description": "Time period for insights",
                                "default": "day",
                            },
                        },
                        "additionalProperties": False,
                    },
                ),
                Tool(
                    name="validate_access_token",
                    description="Validate the Instagram API access token & permissions.",
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                ),
            ]

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent]:
            global instagram_client
            if not instagram_client:
                instagram_client = InstagramClient()

            def _wrap(result: MCPToolResult) -> Sequence[TextContent]:
                return [TextContent(type="text", text=json.dumps(result.dict(), indent=2, default=str))]

            try:
                if name == "get_profile_info":
                    account_id = arguments.get("account_id")
                    profile = await instagram_client.get_profile_info(account_id)
                    return _wrap(MCPToolResult(
                        success=True,
                        data=profile.dict(),
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "get_media_posts":
                    account_id = arguments.get("account_id")
                    limit = arguments.get("limit", 25)
                    after = arguments.get("after")
                    posts = await instagram_client.get_media_posts(account_id, limit, after)
                    return _wrap(MCPToolResult(
                        success=True,
                        data={"posts": [p.dict() for p in posts], "count": len(posts)},
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "get_media_insights":
                    media_id = arguments["media_id"]
                    metrics = arguments.get("metrics")
                    if metrics:
                        metrics = [InsightMetric(m) for m in metrics]
                    insights = await instagram_client.get_media_insights(media_id, metrics)
                    return _wrap(MCPToolResult(
                        success=True,
                        data={"media_id": media_id, "insights": [i.dict() for i in insights]},
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "publish_media":
                    image_url = arguments.get("image_url")
                    video_url = arguments.get("video_url")
                    # Claude-safe validation (since we removed anyOf in schema)
                    if bool(image_url) == bool(video_url):
                        return _wrap(MCPToolResult(
                            success=False,
                            error="Exactly one of image_url or video_url is required.",
                            metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                        ))
                    request = PublishMediaRequest(**arguments)
                    response = await instagram_client.publish_media(request)
                    return _wrap(MCPToolResult(
                        success=True,
                        data=response.dict(),
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "get_account_pages":
                    pages = await instagram_client.get_account_pages()
                    return _wrap(MCPToolResult(
                        success=True,
                        data={"pages": [pg.dict() for pg in pages], "count": len(pages)},
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "get_account_insights":
                    account_id = arguments.get("account_id")
                    metrics = arguments.get("metrics")
                    period = InsightPeriod(arguments.get("period", "day"))
                    insights = await instagram_client.get_account_insights(account_id, metrics, period)
                    return _wrap(MCPToolResult(
                        success=True,
                        data={"insights": [i.dict() for i in insights], "period": period.value},
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                elif name == "validate_access_token":
                    is_valid = await instagram_client.validate_access_token()
                    return _wrap(MCPToolResult(
                        success=True,
                        data={"valid": is_valid},
                        metadata={"tool": name, "timestamp": datetime.utcnow().isoformat()},
                    ))

                else:
                    return _wrap(MCPToolResult(success=False, error=f"Unknown tool: {name}"))

            except InstagramAPIError as e:
                logger.error("Instagram API error", tool=name, error=str(e))
                return _wrap(MCPToolResult(
                    success=False,
                    error=f"Instagram API error: {e.message}",
                    metadata={"error_code": e.error_code, "error_subcode": e.error_subcode},
                ))
            except Exception as e:
                logger.error("Tool execution error", tool=name, error=str(e))
                return _wrap(MCPToolResult(success=False, error=f"Tool execution failed: {str(e)}"))

        @self.server.list_resources()
        async def handle_list_resources() -> List[Resource]:
            return [
                Resource(
                    uri="instagram://profile",
                    name="Instagram Profile",
                    description="Current Instagram business profile information",
                    mimeType="application/json",
                ),
                Resource(
                    uri="instagram://media/recent",
                    name="Recent Media Posts",
                    description="Recent Instagram posts with engagement metrics",
                    mimeType="application/json",
                ),
                Resource(
                    uri="instagram://insights/account",
                    name="Account Insights",
                    description="Account-level analytics and insights",
                    mimeType="application/json",
                ),
                Resource(
                    uri="instagram://pages",
                    name="Connected Pages",
                    description="Facebook pages connected to the account",
                    mimeType="application/json",
                ),
            ]

        @self.server.read_resource()
        async def handle_read_resource(uri: str) -> str:
            global instagram_client
            if not instagram_client:
                instagram_client = InstagramClient()

            try:
                if uri == "instagram://profile":
                    profile = await instagram_client.get_profile_info()
                    return json.dumps(profile.dict(), indent=2)
                elif uri == "instagram://media/recent":
                    posts = await instagram_client.get_media_posts(limit=10)
                    return json.dumps([p.dict() for p in posts], indent=2)
                elif uri == "instagram://insights/account":
                    insights = await instagram_client.get_account_insights()
                    return json.dumps([i.dict() for i in insights], indent=2)
                elif uri == "instagram://pages":
                    pages = await instagram_client.get_account_pages()
                    return json.dumps([pg.dict() for pg in pages], indent=2)
                else:
                    raise ValueError(f"Unknown resource URI: {uri}")
            except Exception as e:
                logger.error("Resource read error", uri=uri, error=str(e))
                return json.dumps({"error": str(e)}, indent=2)

        @self.server.list_prompts()
        async def handle_list_prompts() -> List[Prompt]:
            return [
                Prompt(
                    name="analyze_engagement",
                    description="Analyze Instagram post engagement and provide insights",
                    arguments=[
                        {"name": "media_id", "description": "Instagram media ID", "required": True},
                        {"name": "comparison_period", "description": "e.g., 'last_week'", "required": False},
                    ],
                ),
                Prompt(
                    name="content_strategy",
                    description="Generate content strategy recommendations based on account performance",
                    arguments=[
                        {"name": "focus_area", "description": "engagement | reach | growth", "required": False},
                        {"name": "time_period", "description": "week | month", "required": False},
                    ],
                ),
                Prompt(
                    name="hashtag_analysis",
                    description="Analyze hashtag performance and suggest improvements",
                    arguments=[{"name": "post_count", "description": "How many recent posts", "required": False}],
                ),
            ]

        @self.server.get_prompt()
        async def handle_get_prompt(name: str, arguments: Dict[str, str]) -> str:
            global instagram_client
            if not instagram_client:
                instagram_client = InstagramClient()

            try:
                if name == "analyze_engagement":
                    media_id = arguments.get("media_id")
                    if not media_id:
                        return "Error: media_id is required for engagement analysis"
                    insights = await instagram_client.get_media_insights(media_id)
                    prompt = f"""
Analyze engagement for Instagram post {media_id}.

Insights:
{json.dumps([i.dict() for i in insights], indent=2)}

Provide:
1) Overall performance
2) Key metrics (reach, likes, comments, shares, saves, plays if applicable)
3) Engagement rate & interpretation
4) Recommendations for future posts
5) Comparison with typical benchmarks
"""
                    return prompt

                elif name == "content_strategy":
                    focus_area = arguments.get("focus_area", "engagement")
                    time_period = arguments.get("time_period", "week")
                    posts = await instagram_client.get_media_posts(limit=20)
                    account_insights = await instagram_client.get_account_insights()
                    prompt = f"""
Create a content strategy focusing on {focus_area} over the next {time_period}.

Recent Posts:
{json.dumps([p.dict() for p in posts[:5]], indent=2)}

Account Insights:
{json.dumps([i.dict() for i in account_insights], indent=2)}

Include:
- Performance analysis
- Posting times & frequency
- Content type recommendations
- Caption & hashtag strategy
- Engagement tactics to improve {focus_area}
- Action items for the next {time_period}
"""
                    return prompt

                elif name == "hashtag_analysis":
                    post_count = int(arguments.get("post_count", "10"))
                    posts = await instagram_client.get_media_posts(limit=post_count)
                    hashtags_data = []
                    for post in posts:
                        if post.caption:
                            tags = [w for w in post.caption.split() if w.startswith("#")]
                            hashtags_data.append(
                                {"post_id": post.id, "hashtags": tags, "likes": post.like_count, "comments": post.comments_count}
                            )
                    prompt = f"""
Analyze hashtag performance for the last {post_count} posts.

Data:
{json.dumps(hashtags_data, indent=2)}

Provide:
- Most frequent tags
- Correlation with engagement
- Hashtag diversity
- Optimization recommendations
- New tags to test
"""
                    return prompt

                else:
                    return f"Error: Unknown prompt '{name}'"
            except Exception as e:
                logger.error("Prompt generation error", prompt=name, error=str(e))
                return f"Error generating prompt: {str(e)}"

    async def run(self):
        logger.info("Starting Instagram MCP Server", version=self.settings.mcp_server_version)

        global instagram_client
        instagram_client = InstagramClient()

        try:
            is_valid = await instagram_client.validate_access_token()
            if not is_valid:
                logger.error("Invalid Instagram access token")
                sys.exit(1)
            logger.info("Instagram access token validated successfully")
        except Exception as e:
            logger.error("Failed to validate access token", error=str(e))
            sys.exit(1)

        async with stdio_server() as (read_stream, write_stream):
            # Version-agnostic notification object (duck-typed)
            notif = SimpleNamespace(resources_changed=False, prompts_changed=False, tools_changed=False)
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=self.settings.mcp_server_name,
                    server_version=self.settings.mcp_server_version,
                    capabilities=self.server.get_capabilities(
                        notification_options=notif, experimental_capabilities={}
                    ),
                ),
            )


async def main():
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    import logging
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level))

    server = InstagramMCPServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
