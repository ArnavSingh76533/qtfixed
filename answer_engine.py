"""Shared regular/inline answer routing, including current-context web delegation."""
import re
import config
from provider import WebSearchRequested, ProviderError

CURRENT = re.compile(r'\b(today|tonight|latest|current|currently|recent|breaking|news|weather|live score|search (?:the )?web|look (?:it|this) up)\b',re.I)


async def answer_stream(groq, web, messages, reasoning='medium', force_web=False, on_status=None):
    from image_generation import ImageRequested, image_intent, available
    prompt=messages[-1]['content']
    async def search(query):
        if not config.WEB_ENABLED or web is None:
            raise ProviderError('Web search is disabled. Current facts were not verified.')
        if on_status:await on_status('🌐 Searching the web…')
        return await web.search(query)
    if not force_web and image_intent(prompt):
        raise ImageRequested(prompt)
    if force_web:
        yield await search(prompt)
        return
    # Clear time-sensitive wording does not depend on a model tool-call decision.
    if config.WEB_ENABLED and CURRENT.search(prompt):
        yield await search(prompt)
        return
    try:
        async for part in groq.stream(messages,reasoning=reasoning,allow_web=config.WEB_ENABLED,allow_image=available()):
            yield part
    except WebSearchRequested as request:
        yield '\n\n' + await search(request.query)
