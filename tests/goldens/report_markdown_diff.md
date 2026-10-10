## Linkkeep: https://example.com

:white_check_mark: **Run #42** `completed` - 2026-03-04 09:00 UTC, 12.3s

| severity | count | pages | links checked | robots-blocked |
| --- | --- | --- | --- | --- |
| :x: errors | 5 | 6 | 41 | 1 |
| :warning: warnings | 4 | | | |
| :information_source: info | 3 | | | |

> [!NOTE]
> sitemap added 3 URL(s)

### Issues

| | issue | target | status | source page |
| --- | --- | --- | --- | --- |
| :x: | `broken` | [`https://example.com/shop/checkout`](https://example.com/shop/checkout) | 500 | [`https://example.com/`](https://example.com/) |
| :x: | `broken` | [`https://example.com/docs/handbook.pdf`](https://example.com/docs/handbook.pdf) | 404 | [`https://example.com/blog/post-1`](https://example.com/blog/post-1) |
| :x: | `redirect_loop` | [`https://example.com/forum/thread/7`](https://example.com/forum/thread/7) | 301 | [`https://example.com/forum`](https://example.com/forum) |
| :x: | `redirect_to_error` | [`https://example.com/click/99`](https://example.com/click/99) | 410 | [`https://example.com/promo`](https://example.com/promo) |
| :x: | `unreachable` | [`https://cdn.example.test/widgets.js`](https://cdn.example.test/widgets.js) | - | [`https://example.com/about`](https://example.com/about) |
| :warning: | `missing_fragment` | [`https://example.com/docs/api#authentication`](https://example.com/docs/api#authentication) | - | [`https://example.com/docs`](https://example.com/docs) |
| :warning: | `mixed_content` | [`http://cdn.example.com/banner.png`](http://cdn.example.com/banner.png) | - | [`https://example.com/pricing`](https://example.com/pricing) |
| :warning: | `redirect_chain` | [`https://example.com/old/news`](https://example.com/old/news) | 200 | [`https://example.com/legacy`](https://example.com/legacy) |
| :warning: | `redirect_http_downgrade` | [`https://example.com/login`](https://example.com/login) | 200 | [`https://example.com/secure`](https://example.com/secure) |
| :information_source: | `blocked_by_robots` | [`https://example.com/admin/users`](https://example.com/admin/users) | - | [`https://example.com/internal`](https://example.com/internal) |
| :information_source: | `redirect_temporary_permanent_internal` | [`https://example.com/news-2024`](https://example.com/news-2024) | 302 | [`https://example.com/`](https://example.com/) |
| :information_source: | `slow` | [`https://example.com/reports/q1`](https://example.com/reports/q1) | 200 | [`https://example.com/`](https://example.com/) |

### Details

<details><summary>:link: <code>broken</code> (2) - Target responded with a 4xx/5xx status</summary>

- **from** [`https://example.com/`](https://example.com/)
  - [`https://example.com/shop/checkout`](https://example.com/shop/checkout) - HTTP 500, label `Checkout`
- **from** [`https://example.com/blog/post-1`](https://example.com/blog/post-1)
  - [`https://example.com/docs/handbook.pdf`](https://example.com/docs/handbook.pdf) - HTTP 404, label `Handbook`

</details>

<details><summary>:cyclone: <code>redirect_loop</code> (1) - Redirect chain loops back to a URL already seen</summary>

- **from** [`https://example.com/forum`](https://example.com/forum)
  - [`https://example.com/forum/thread/7`](https://example.com/forum/thread/7) - HTTP 301

</details>

<details><summary>:arrow_heading_down: <code>redirect_to_error</code> (1) - Redirect chain ends in a 4xx/5xx status</summary>

- **from** [`https://example.com/promo`](https://example.com/promo)
  - [`https://example.com/click/99`](https://example.com/click/99) - HTTP 410, label `Summer sale`

</details>

<details><summary>:satellite: <code>unreachable</code> (1) - Target could not be reached at all</summary>

- **from** [`https://example.com/about`](https://example.com/about)
  - [`https://cdn.example.test/widgets.js`](https://cdn.example.test/widgets.js) - DNS failure: Name or service not known
    - DNS failure: Name or service not known

</details>

<details><summary>:target: <code>missing_fragment</code> (1) - The #fragment does not exist on the target page</summary>

- **from** [`https://example.com/docs`](https://example.com/docs)
  - [`https://example.com/docs/api#authentication`](https://example.com/docs/api#authentication) - no id or name 'authentication' on the target page, label `Auth`
    - no id or name 'authentication' on the target page

</details>

<details><summary>:unlock: <code>mixed_content</code> (1) - HTTP resource linked from an HTTPS page</summary>

- **from** [`https://example.com/pricing`](https://example.com/pricing)
  - [`http://cdn.example.com/banner.png`](http://cdn.example.com/banner.png) - insecure http resource on an https page, label `Banner`
    - insecure http resource on an https page

</details>

<details><summary>:arrows_counterclockwise: <code>redirect_chain</code> (1) - Too many redirects in the chain</summary>

- **from** [`https://example.com/legacy`](https://example.com/legacy)
  - [`https://example.com/old/news`](https://example.com/old/news) - HTTP 200, label `News`

</details>

<details><summary>:lock: <code>redirect_http_downgrade</code> (1) - HTTPS URL redirects down to HTTP</summary>

- **from** [`https://example.com/secure`](https://example.com/secure)
  - [`https://example.com/login`](https://example.com/login) - HTTP 200, label `Log in`

</details>

<details><summary>:robot: <code>blocked_by_robots</code> (1) - robots.txt prevented Linkkeep from crawling this URL</summary>

- **from** [`https://example.com/internal`](https://example.com/internal)
  - [`https://example.com/admin/users`](https://example.com/admin/users) - robots.txt disallows Linkkeep for this URL, so it was not crawled or checked
    - robots.txt disallows Linkkeep for this URL, so it was not crawled or checked

</details>

<details><summary>:hourglass: <code>redirect_temporary_permanent_internal</code> (1) - Internal link uses a temporary redirect</summary>

- **from** [`https://example.com/`](https://example.com/)
  - [`https://example.com/news-2024`](https://example.com/news-2024) - HTTP 302

</details>

<details><summary>:turtle: <code>slow</code> (1) - Target is slower than the configured threshold</summary>

- **from** [`https://example.com/`](https://example.com/)
  - [`https://example.com/reports/q1`](https://example.com/reports/q1) - HTTP 200, label `Q1 report`

</details>


### Changes since the previous crawl

comparing run #41 (2026-02-25 09:00) with run #42 (2026-03-04 09:00)

:rotating_light: **11 new problem(s):**

| | issue | target | status | source page |
| --- | --- | --- | --- | --- |
| :x: | `broken` | [`https://example.com/shop/checkout`](https://example.com/shop/checkout) | 500 | [`https://example.com/`](https://example.com/) |
| :x: | `redirect_loop` | [`https://example.com/forum/thread/7`](https://example.com/forum/thread/7) | 301 | [`https://example.com/forum`](https://example.com/forum) |
| :x: | `redirect_to_error` | [`https://example.com/click/99`](https://example.com/click/99) | 410 | [`https://example.com/promo`](https://example.com/promo) |
| :x: | `unreachable` | [`https://cdn.example.test/widgets.js`](https://cdn.example.test/widgets.js) | - | [`https://example.com/about`](https://example.com/about) |
| :warning: | `missing_fragment` | [`https://example.com/docs/api#authentication`](https://example.com/docs/api#authentication) | - | [`https://example.com/docs`](https://example.com/docs) |
| :warning: | `mixed_content` | [`http://cdn.example.com/banner.png`](http://cdn.example.com/banner.png) | - | [`https://example.com/pricing`](https://example.com/pricing) |
| :warning: | `redirect_chain` | [`https://example.com/old/news`](https://example.com/old/news) | 200 | [`https://example.com/legacy`](https://example.com/legacy) |
| :warning: | `redirect_http_downgrade` | [`https://example.com/login`](https://example.com/login) | 200 | [`https://example.com/secure`](https://example.com/secure) |
| :information_source: | `blocked_by_robots` | [`https://example.com/admin/users`](https://example.com/admin/users) | - | [`https://example.com/internal`](https://example.com/internal) |
| :information_source: | `redirect_temporary_permanent_internal` | [`https://example.com/news-2024`](https://example.com/news-2024) | 302 | [`https://example.com/`](https://example.com/) |
| :information_source: | `slow` | [`https://example.com/reports/q1`](https://example.com/reports/q1) | 200 | [`https://example.com/`](https://example.com/) |

:checkered_flag: **2 fixed:**

| | issue | target | status | source page |
| --- | --- | --- | --- | --- |
| :x: | `broken` | [`https://example.com/tmp/banner.html`](https://example.com/tmp/banner.html) | 404 | [`https://example.com/`](https://example.com/) |
| :warning: | `missing_fragment` | [`https://example.com/docs/api#legacy`](https://example.com/docs/api#legacy) | - | [`https://example.com/docs`](https://example.com/docs) |

:hourglass: **1 still broken:**

- `broken` [`https://example.com/docs/handbook.pdf`](https://example.com/docs/handbook.pdf) - broken since 2026-02-25 (7 days), seen in 4 runs
