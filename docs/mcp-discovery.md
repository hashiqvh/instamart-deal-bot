# MCP discovery — 2026-10-30

No Swiggy/Instamart connector was exposed in this Codex session; plugin discovery returned no matching plugin. More integrations may be available in the [plugin directory](https://chatgpt.com/plugins). The repository was initially empty and contained no MCP integration/configuration. No credentials were accessed or assumed.

Primary sources inspected before implementation:

- [Official server manifest](https://github.com/Swiggy/swiggy-mcp-server-manifest)
- [Instamart tools](https://mcp.swiggy.com/builders/docs/reference/instamart/)
- [Search schema](https://mcp.swiggy.com/builders/docs/reference/instamart/search_products/)
- [Coupon prerequisites](https://mcp.swiggy.com/builders/docs/reference/instamart/list_coupons/)
- [Rate limits](https://mcp.swiggy.com/builders/docs/operate/rate-limits/)
- [Authentication](https://mcp.swiggy.com/builders/docs/start/authenticate/)

The current documented endpoint is `https://mcp.swiggy.com/im`. Search accepts a nonempty query and guest latitude/longitude; authenticated address IDs are an alternative. Output includes SKU variations, MRP, offer price, image and availability. Pagination returns a string nextOffset although the input offset is numeric; this adapter accepts numeric cursors and fails on incompatible cursors. No full-catalog discovery or separate product-detail tool is documented. Direct product URLs aren't part of the published output contract.

Coupons are cart-dependent and not universally rolled out. This bot doesn't create carts to discover them. Search offer prices are the applicable item-level offer source; promotional badges aren't added to the computed savings.

Gateway quotas are not numerically published. There is no documented search polling cadence guarantee. Conservative pacing, Retry-After handling and bounded queries/pages are implementation choices, not promised provider limits. Confirm background polling traffic with Swiggy before scaling.

These are documentation-verified capabilities, not a claim of authenticated live verification. Runtime `tools/list` and JSON Schema validation gate each search. Run `instadeals --inspect` after provisioning a token, then validate a real location and compare returned prices with Instamart before relying on alerts. Guest-coordinate support may differ by account/rollout; incompatibility fails closed. Price values are interpreted as rupees per the provider-facing price representation; verify units against a live product during commissioning.

## Authenticated verification

Subsequent local setup successfully completed Swiggy OAuth in Brave and authenticated to the live MCP server. `tools/list` confirmed `search_products` with query, latitude, longitude, addressId and numeric offset (0–10000); only query is schema-required. Product payloads and currency units still need verification at a user-selected location.
