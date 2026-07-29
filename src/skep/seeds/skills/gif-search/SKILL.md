---
name: gif-search
description: find the right GIF via Tenor's public API
---

# GIF search

Tools: read_url, allow_fetch_domain, search_web

1. Tenor's public v2 API serves JSON search results
   (tenor.googleapis.com — needs the user's free API key in the URL;
   offer `allow_fetch_domain` once). Without a key, `search_web`
   "<thing> gif tenor" and read the result page.
2. `read_url` the search endpoint with the query; pick by the content
   description, not position — the first hit is rarely the joke.
3. Deliver the direct .gif URL (the media url in the JSON) so it
   embeds wherever the user pastes it; offer 2-3 options with one-line
   vibes ("deadpan", "celebratory").
4. Discord replies: the channel layer renders plain URLs — just send
   the link.
