---
title: Data sources
description: The public registers behind the API, their licence, and how often the data updates.
---

The addresses, properties and places the API returns come from the official Danish registers,
published on [Datafordeleren](https://datafordeler.dk). None of it is scraped or crowd-sourced.

## Registers

| Register | What it provides |
| --- | --- |
| DAR, Danmarks Adresseregister | Addresses, roads, postnumre and supplerende bynavne. |
| DAGI, Danmarks Administrative Geografiske Inddeling | Kommune, region, sogn, retskreds, politikreds and opstillingskreds polygons, and the postnummer polygons. |
| MAT, Matriklen | Jordstykker, samlede faste ejendomme, ejerlejligheder, bygninger på fremmed grund and ejerlav. |
| EBR, Ejendomsbeliggenhedsregisteret | The link from an address to the property it sits on. |
| DS, Danske Stednavne | Named places. |

## Attribution

All of it is free data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). If you
publish anything derived from a response, credit Klimadatastyrelsen, the authority behind the data.
A line such as `Adressedata: Klimadatastyrelsen, CC BY 4.0` covers it.

## Updates

Data updates on its own, once a day. Each register publishes the day's changes, and Bifrost picks
them up and serves them a few minutes later.

A new or changed address usually shows up here a day or two after it reaches the register. The
update does not run at a fixed hour, so it lands at a slightly different time each day.

You do not have to take this on trust. Every response carries the exact time the data behind it was
last refreshed, in the [`X-Bifrost-Data-Updated` header](./results/#response-headers).

## Coverage

The served dataset currently holds roughly:

- 3.9 million current addresses, across 109,000 roads
- 2.6 million properties
- 145,000 place names
- 4,000 administrative areas

Another 185,000 retired and 13,000 preliminary addresses are in there too, reachable by widening
[`lifecycle`](./resolve/#lifecycle).
