"""
logo.py -- the docjuice logo, rendered in colour in the terminal.

The art is stored exactly as supplied (BBCode colour markup) and parsed at
import time, so swapping the logo only means replacing RAW_BBCODE.

Also runnable on its own -- the installer uses this, before the virtual
environment exists, so it depends on nothing outside the standard library:

    python3 logo.py            # render for a dark terminal (the default)
    python3 logo.py --light    # render the original colours
"""

from __future__ import annotations

import colorsys
import html
import os
import re
import shutil
import sys
from typing import List, Optional, Tuple

RAW_BBCODE = r"""[size=9px][font=monospace][color=#7f7f7f]<span style="color:#7f7f7f"> [/color]</span>
[color=#7f7f7f][/color][color=#7f7f7f] [/color]
[color=#7f7f7f][/color][color=#7f7f7f] [/color]
[color=#7f7f7f][/color][color=#7f7f7f] [/color]
[color=#7f7f7f][/color][color=#7f7f7f] [/color]
[color=#7f7f7f][/color][color=#7f7f7f]                        [/color][color=#2c3236]█[/color][color=#202a30]█[/color][color=#20292e]█[/color][color=#20292e]█[/color][color=#20292e]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#212930]█[/color][color=#212930]█[/color][color=#212930]█[/color][color=#212930]█[/color][color=#202930]█[/color][color=#202930]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#21292f]█[/color][color=#202930]█[/color][color=#202930]█[/color][color=#202930]█[/color][color=#202930]█[/color][color=#20292f]█[/color][color=#20292e]█[/color][color=#20292f]█[/color][color=#20292f]█[/color][color=#464a4d]▌[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#2b3135]█[/color][color=#293338]█[/color][color=#55585b]░[/color][color=#5e6162]²[/color][color=#5f5f60]²[/color][color=#5e6060]²[/color][color=#5e6060]²[/color][color=#5e6160]²[/color][color=#5e6160]²[/color][color=#5e6161]²[/color][color=#5e6160]²[/color][color=#5e6061]²[/color][color=#5e6061]²[/color][color=#5e6061]²[/color][color=#5e6061]²[/color][color=#5e6062]²[/color][color=#5e6062]²[/color][color=#5e6062]²[/color][color=#5e6162]²[/color][color=#5e6062]²[/color][color=#5e6162]²[/color][color=#5e6162]²[/color][color=#5e6162]²[/color][color=#5e6162]²[/color][color=#5e6162]²[/color][color=#4d5256]▐[/color][color=#0c171e]▓[/color][color=#19232c]▓[/color][color=#3e4753]▒[/color][color=#363f48]▒[/color][color=#2a3238]█[/color][color=#2e363a]█[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0c151c]▓[/color][color=#0b171e]▓                       [/color][color=#6c6f6f]][/color][color=#0b161e]▓[/color][color=#222c33]▓[/color][color=#5c646e]░[/color][color=#525b65]░[/color][color=#3b4651]▒[/color][color=#313c45]▒[/color][color=#2a3439]█[/color][color=#2c3438]█[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0c161d]▓[/color][color=#0b161a]▓    [/color][color=#6e7374]][/color][color=#555e69]║[/color][color=#555e67]║[/color][color=#555e67]║[/color][color=#555e67]║[/color][color=#555e67]║[/color][color=#555e67]║[/color][color=#555e67]║[/color][color=#555e66]║[/color][color=#555e66]║[/color][color=#555e67]║[/color][color=#555e66]║[/color][color=#565f67]║[/color][color=#565e66]║     [/color][color=#6d7171]][/color][color=#0b141c]▓[/color][color=#242e35]▓[/color][color=#61686f]k[/color][color=#5e666f]░[/color][color=#5b636e]░[/color][color=#525b66]░[/color][color=#3a4650]▒[/color][color=#364048]▒[/color][color=#2d353a]█[/color][color=#2e353a]█[/color]
[color=#7f7f7f][/color][color=#7f7f7f]            [/color][color=#b0894b]╓[/color][color=#b1894a]╓[/color][color=#938565].       [/color][color=#0c161c]▓[/color][color=#0c151a]▓     [/color][color=#6c6d6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6e6f]`[/color][color=#6b6f71]`[/color][color=#6c6e72]`     [/color][color=#6e7173]][/color][color=#0c171e]▓[/color][color=#232e35]▓[/color][color=#616970])[/color][color=#606870]([/color][color=#616970]z[/color][color=#5f6871]([/color][color=#5a626b]░[/color][color=#576069]░[/color][color=#36424e]▒[/color][color=#35404a]▒[/color][color=#2f373c]█[/color][color=#333a3f]█[/color]
[color=#7f7f7f][/color][color=#7f7f7f]           [/color][color=#9c875d]][/color][color=#f2950c]░[/color][color=#f0970c]░[/color][color=#cd902b]╖[/color][color=#aa864f]╓      [/color][color=#0c161c]▓[/color][color=#0c161b]▓    [/color][color=#717374]][/color][color=#5b6269]╖[/color][color=#5a6269]╖[/color][color=#5a6269]╖[/color][color=#5a6168]╖[/color][color=#5a6168]╖[/color][color=#5a6168]╖[/color][color=#5a6269]╖[/color][color=#5a6268]╖[/color][color=#5a6268]╖[/color][color=#5a6269]╖[/color][color=#5a6269]╖[/color][color=#5a626a]╖[/color][color=#5a626a]╖[/color][color=#5b626a]╖[/color][color=#5b646c]╖[/color][color=#5e6368]╓   [/color][color=#505452]╩[/color][color=#434a4e]▐[/color][color=#2a343b]█[/color][color=#2a343b]█[/color][color=#2a343b]█[/color][color=#2a353c]█[/color][color=#2b353c]█[/color][color=#283339]█[/color][color=#212c35]█[/color][color=#212c35]█[/color][color=#0c1920]▓[/color][color=#0c171e]▓[/color][color=#30383d]█[/color][color=#373e42]█[/color]
[color=#7f7f7f][/color][color=#7f7f7f]            [/color][color=#a28859]`[/color][color=#a28758]`[/color][color=#e69311]░[/color][color=#ee950d]░[/color][color=#c68d33]╓[/color][color=#ae894d]╓    [/color][color=#0c161c]▓[/color][color=#0b171c]▓    [/color][color=#787673]'[/color][color=#666a6b]╙[/color][color=#676b6d]╙[/color][color=#676a6d]╙[/color][color=#676a6d]╙[/color][color=#666a6c]╙[/color][color=#676a6c]╙[/color][color=#666a6b]╙[/color][color=#666a6b]╙[/color][color=#676a6c]╙[/color][color=#676a6c]╙[/color][color=#676a6c]╙[/color][color=#676a6c]╙[/color][color=#676a6c]╙[/color][color=#676a6c]╙[/color][color=#666b6a]╙[/color][color=#6a6b6c]`     [/color][color=#4e5251]²[/color][color=#4f5252]²[/color][color=#4e5152]²[/color][color=#4e5151]²[/color][color=#4e5151]²[/color][color=#4e5152]²[/color][color=#4c4f4f]▀[/color][color=#464948]▀[/color][color=#464847]▀[/color][color=#474949]▀[/color][color=#0d171d]▓[/color][color=#0c181e]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]              [/color][color=#a48552]╙[/color][color=#af884a]╙[/color][color=#da911f]║[/color][color=#f0950c]░    [/color][color=#0c151c]▓[/color][color=#0b171c]▓    [/color][color=#747472]][/color][color=#5e6569]╓[/color][color=#5e6367]╖[/color][color=#5f6467]╖[/color][color=#5f6467]╓[/color][color=#5f6467]╖[/color][color=#5f6468]╖[/color][color=#5f6467]╓[/color][color=#5e6467]╖[/color][color=#5f6467]╖[/color][color=#5f6467]╖[/color][color=#5f6467]╖[/color][color=#5f6467]╖[/color][color=#5f6367]╖[/color][color=#5f6467]╖[/color][color=#5f6467]╓[/color][color=#5f6468]╖[/color][color=#5f6568]╖[/color][color=#606569]╖[/color][color=#666868]w         [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c]`[/color][color=#0d171d]▓[/color][color=#0c171d]▓[/color][color=#666a6b]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]         [/color][color=#bf8f3b]╓[/color][color=#be8b3b]╓[/color][color=#bf8c3a]╓[/color][color=#9d855c]⌐    [/color][color=#9a8461]`    [/color][color=#0c161d]▓[/color][color=#0b161c]▓    [/color][color=#747472]'[/color][color=#63676a]╙[/color][color=#62676a]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#62666a]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#626669]╙[/color][color=#626569]╙[/color][color=#636669]╙[/color][color=#626569]╙[/color][color=#626669]╙[/color][color=#62666a]╙[/color][color=#62666a]╙[/color][color=#63676a]╙[/color][color=#6b6c6c]`         [/color][color=#74716c],[/color][color=#74716c],[/color][color=#73716c],[/color][color=#0d171d]▓[/color][color=#0c181d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]         [/color][color=#b68e42]╙[/color][color=#b98c41]╙[/color][color=#e79312]░[/color][color=#d49225]╖[/color][color=#b98a42]╓[/color][color=#ba8a3f]╓       [/color][color=#0c161d]▓[/color][color=#0c171c]▓                                 [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c],[/color][color=#0d171d]▓[/color][color=#0c181d]▓[/color][color=#666a6b]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]           [/color][color=#ad8849]╙[/color][color=#bb8c41]╙[/color][color=#da901f]║[/color][color=#f1920c]░       [/color][color=#0c161d]▓[/color][color=#0c171d]▓                                 [/color][color=#75726d],[/color][color=#74716c],[/color][color=#74716d]:[/color][color=#0d171c]▓[/color][color=#0c181d]▓[/color][color=#666a6b]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]              [/color][color=#9c855e]`       [/color][color=#0c161d]▓[/color][color=#0b161c]▓      [/color][color=#0c161e]▓[/color][color=#0b171f]▓[/color][color=#11191f]▓            [/color][color=#0d161d]▓[/color][color=#0b161d]▓[/color][color=#272e32]▌         [/color][color=#75726d],[/color][color=#74716c],[/color][color=#75726d],[/color][color=#0d171d]▓[/color][color=#0d181e]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0c161e]▓[/color][color=#0b171e]▓      [/color][color=#0b161e]▓[/color][color=#0c171f]▓[/color][color=#10181f]▓            [/color][color=#0d161e]▓[/color][color=#0c171d]▓[/color][color=#262e31]▌         [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c],[/color][color=#0d171c]▓[/color][color=#0d181e]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0c161d]▓[/color][color=#0c161c]▓      [/color][color=#1d252b]▓[/color][color=#1b252d]▓[/color][color=#21282d]▓            [/color][color=#1d242a]▓[/color][color=#1c242a]▓[/color][color=#33393b]▌         [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c],[/color][color=#0d171c]▓[/color][color=#0c171d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0c151c]▓[/color][color=#0b161c]▓      [/color][color=#8d7c6c],[/color][color=#8f7c6b],[/color][color=#8e7b6b],[/color][color=#8d7a6c],[/color][color=#8e7c6a],[/color][color=#443632]╢[/color][color=#131217]▓[/color][color=#12171d]▓[/color][color=#181720]▓[/color][color=#1a1720]▓[/color][color=#0f181e]▓[/color][color=#0b171e]▓[/color][color=#525554]▌              [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c],[/color][color=#0c161c]▓[/color][color=#0c171d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                      [/color][color=#0d151c]▓[/color][color=#0b181e]▓   [/color][color=#99775e],[/color][color=#a76e4c]╓[/color][color=#b2683f]╓[/color][color=#ca5d27]╝[/color][color=#cc5b26]╝[/color][color=#c85424]Ñ[/color][color=#c65223]▒[/color][color=#c55125]Ñ[/color][color=#964626]▒[/color][color=#633829]▀[/color][color=#5d2f35]▒[/color][color=#873541]▄[/color][color=#863845]▄[/color][color=#372b31]▓[/color][color=#282e35]▀[/color][color=#5d6160]C              [/color][color=#74716c],[/color][color=#74716c],[/color][color=#74716c],[/color][color=#0c161c]▓[/color][color=#0c171d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                [/color][color=#6a6c6d],[/color][color=#5b5f61]g[/color][color=#2e353d]█[/color][color=#121616]▓[/color][color=#3c2813]▓[/color][color=#3c2915]▓[/color][color=#1b1812]▓[/color][color=#151712]▓[/color][color=#484a4c]▄[/color][color=#505655]▄[/color][color=#825c45]▄[/color][color=#e7630d]▒[/color][color=#bf4f27]▒[/color][color=#a7533d]╨[/color][color=#857c72].[/color][color=#7f7b74].[/color][color=#787570],[/color][color=#77746e],[/color][color=#7c7972]`  [/color][color=#515353]╚[/color][color=#343b3f]▀[/color][color=#343a3d]▀[/color][color=#696a69]─             [/color][color=#70716d],[/color][color=#525454]▄[/color][color=#61615e]µ[/color][color=#75726b],[/color][color=#74726b],[/color][color=#73716c],[/color][color=#0c171c]▓[/color][color=#0c171d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                [/color][color=#373f43]╢[/color][color=#0c151b]▓[/color][color=#836e4a]W[/color][color=#967a50],[/color][color=#aa814e],[/color][color=#b7803c]╙[/color][color=#d17b25]╝[/color][color=#ce7a26]╝[/color][color=#90500e]▒[/color][color=#8f520f]▒[/color][color=#b9500c]▒[/color][color=#e34b0b]▒[/color][color=#562c21]█[/color][color=#2d3236]█[/color][color=#2b3134]█[/color][color=#2a3132]█[/color][color=#6c6c67]M                     [/color][color=#545758]▐[/color][color=#0b171f]▓[/color][color=#232c31]█[/color][color=#444647]▄[/color][color=#656561]∩[/color][color=#76726b]`[/color][color=#0e171d]▓[/color][color=#0c171d]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                [/color][color=#30383c]╢[/color][color=#0c1417]▓[/color][color=#de8911]░[/color][color=#ee910d]░[/color][color=#f1900c]░[/color][color=#d48e23]░[/color][color=#b88843]╓[/color][color=#b88942]╓[/color][color=#b78a44]╓[/color][color=#b18544]╓[/color][color=#aa563a]╨[/color][color=#ac543c]╜[/color][color=#b76332]╝[/color][color=#bb6a2e]╝[/color][color=#b9672e]░[/color][color=#af6130]░[/color][color=#1e1d19]▓[/color][color=#0c141d]▓                     [/color][color=#5b5c5a]²[/color][color=#393e3e]▐[/color][color=#0c171e]▓[/color][color=#31383b]█[/color][color=#414444]▄[/color][color=#0d171d]▓[/color][color=#0d181e]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]              [/color][color=#1b2629]█[/color][color=#161d24]█[/color][color=#0f1a20]▓[/color][color=#0b191c]▓[/color][color=#3d2f13]█[/color][color=#754e10]▌[/color][color=#f0910e]░[/color][color=#ef930d]░[/color][color=#f0920c]░[/color][color=#f0930c]░[/color][color=#f0910c]░[/color][color=#ec900f]░[/color][color=#d38b29]║[/color][color=#d38d27]║ [/color][color=#af6741]║[/color][color=#d55918]╢[/color][color=#e55409]▒[/color][color=#311d12]▓[/color][color=#0d161d]▓                      [/color][color=#656560]╙[/color][color=#3b403f]▀[/color][color=#30383c]▒[/color][color=#0d171f]▓[/color][color=#0c171d]▓[/color][color=#0c171e]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]            [/color][color=#1d2428]▓[/color][color=#0b171d]▓[/color][color=#747473],  [/color][color=#6e7070]][/color][color=#0a141b]▓[/color][color=#372d11]▓[/color][color=#ef950d]░[/color][color=#e3920e]░[/color][color=#d0940f]░[/color][color=#4c7513]▒[/color][color=#277416]█[/color][color=#0c691c]▓[/color][color=#70740f]▒[/color][color=#e9920f]░ [/color][color=#c0612e]║[/color][color=#e0530a]▒[/color][color=#d34f0c]▒[/color][color=#301f14]▓[/color][color=#121a20]▓                       [/color][color=#75706b],[/color][color=#575958]▐[/color][color=#0c1820]▓[/color][color=#0d181e]▓[/color][color=#0d1820]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]            [/color][color=#1d2529]▓[/color][color=#0c171f]▓[/color][color=#4d4f4f]▄[/color][color=#4f514f]▄[/color][color=#4f514f]▄[/color][color=#4a4c49]▄[/color][color=#3e2611]▓[/color][color=#664310]▓[/color][color=#e69010]░[/color][color=#428815]▒[/color][color=#068a27]▒[/color][color=#05912c]▒[/color][color=#059829]▒[/color][color=#10681b]▓[/color][color=#857e14]▒ [/color][color=#b2683a]║[/color][color=#dc5d14]▒[/color][color=#903b0d]▒[/color][color=#09161b]▓[/color][color=#5e5c5f]H[/color][color=#75716b],                     [/color][color=#78746e],[/color][color=#76726d]&lt;[/color][color=#77716a],[/color][color=#575959]▐[/color][color=#0c181f]▓[/color][color=#0d181e]▓[/color][color=#0d1820]▓[/color][color=#666a6c]M[/color]
[color=#7f7f7f][/color][color=#7f7f7f]             [/color][color=#686a6e].[/color][color=#111d23]▓[/color][color=#0c161f]▓[/color][color=#512f12]▓[/color][color=#69350d]▓[/color][color=#e87e0e]░[/color][color=#d1810d]░[/color][color=#6c7616]▒[/color][color=#186614]▓[/color][color=#057d2a]▒[/color][color=#2b7b15]▒[/color][color=#498514]▒[/color][color=#c08910]░[/color][color=#db8b1a]░ [/color][color=#d45c15]▒[/color][color=#ea5c09]▒[/color][color=#893d11]▒[/color][color=#0a161c]▓[/color][color=#5e5d5a]░[/color][color=#74716b],[/color][color=#78746e];[/color][color=#78746d]&lt;[/color][color=#77736d]&lt;[/color][color=#76736d]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736d]&lt;[/color][color=#77736d]&lt;[/color][color=#78736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#77736d]&lt;[/color][color=#77736e]&lt;[/color][color=#77736e]&lt;[/color][color=#76746e]&lt;[/color][color=#76716d]&lt;[/color][color=#74716b],[/color][color=#4d5051]▐[/color][color=#1d272c]█[/color][color=#1a242a]█[/color][color=#0c181e]▓[/color][color=#373f42]▀[/color][color=#3f4549]▀[/color]
[color=#7f7f7f][/color][color=#7f7f7f]              [/color][color=#131b22]▓[/color][color=#0c1718]▓[/color][color=#cb6411]░[/color][color=#ed6e09]▒[/color][color=#f08f0e]░[/color][color=#d1880e]░[/color][color=#807511]▒[/color][color=#7f7612]▒[/color][color=#807512]▒[/color][color=#df8f0f]░[/color][color=#ef8e0d]░[/color][color=#e98e12]░[/color][color=#a38456]`[/color][color=#b3693d]╖[/color][color=#df550b]▒[/color][color=#913b0d]▒[/color][color=#381e12]█[/color][color=#0b171f]▓[/color][color=#2c3838]█[/color][color=#34383b]█[/color][color=#33383a]█[/color][color=#34393b]█[/color][color=#34393b]█[/color][color=#33393b]█[/color][color=#33393b]█[/color][color=#343a3c]█[/color][color=#34393c]█[/color][color=#34393c]█[/color][color=#34393b]█[/color][color=#34393b]█[/color][color=#343a3c]█[/color][color=#343a3c]█[/color][color=#33393b]█[/color][color=#33393a]█[/color][color=#343a3c]█[/color][color=#343a3c]█[/color][color=#343a3c]█[/color][color=#343a3b]█[/color][color=#343a3b]█[/color][color=#33393a]█[/color][color=#34393c]█[/color][color=#34393a]█[/color][color=#22292d]█[/color][color=#0c181e]▓[/color][color=#3d454a]▀[/color][color=#4a4e52]▀[/color]
[color=#7f7f7f][/color][color=#7f7f7f]               [/color][color=#6a6d6f].[/color][color=#2d2116]█[/color][color=#2e2315]█[/color][color=#392d12]█[/color][color=#6c4610]█[/color][color=#ee7b0c]░[/color][color=#ed7a0c]░[/color][color=#ed7d0b]░[/color][color=#f1880b]░[/color][color=#f08e0d]░[/color][color=#b58940]H[/color][color=#997058]][/color][color=#e0550c]▒[/color][color=#e5540b]▒[/color][color=#4b2312]▓[/color][color=#0b151c]▓[/color][color=#525659]░[/color][color=#575b5e]²[/color][color=#0d171d]▓[/color][color=#09151b]▓[/color][color=#30373b]▌[/color][color=#575a5b]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5c]²[/color][color=#565b5e]²[/color][color=#0b141a]▓[/color][color=#08141a]▓[/color][color=#4a5052]░[/color][color=#575a5d]²[/color][color=#585b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#585b5d]²[/color][color=#575b5d]²[/color][color=#575b5d]²[/color][color=#575a5c]²[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                   [/color][color=#565858]▐[/color][color=#171a18]▓[/color][color=#191918]▓[/color][color=#191a19]▓[/color][color=#181a17]▓[/color][color=#352c18]█[/color][color=#272a27]█[/color][color=#28211e]█[/color][color=#311e14]█[/color][color=#181516]▓[/color][color=#585556]░   [/color][color=#0f181f]▓[/color][color=#0a171f]▓[/color][color=#454d50]▌          [/color][color=#0e171e]▓[/color][color=#0a161d]▓[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                                 [/color][color=#0f181f]▓[/color][color=#0c1820]▓[/color][color=#454d4f]▌          [/color][color=#10191f]▓[/color][color=#0c171e]▓[/color][color=#4c5357]▄[/color][color=#585b5e]g[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                             [/color][color=#353c40]▒[/color][color=#111c23]▓[/color][color=#111c23]▓[/color][color=#101b22]▓[/color][color=#0c171f]▓[/color][color=#0c171d]▓[/color][color=#404647]▌[/color][color=#797573],[/color][color=#787673],[/color][color=#797673],[/color][color=#797673],[/color][color=#787673],[/color][color=#787673],[/color][color=#787673],[/color][color=#777573],[/color][color=#141e25]▓[/color][color=#101b24]▓[/color][color=#0c171f]▓[/color][color=#0c171f]▓[/color][color=#0c171f]▓[/color][color=#0d1820]▓[/color][color=#717070],[/color]
[color=#7f7f7f][/color][color=#7f7f7f]                        [/color][color=#74716d]&lt;[/color][color=#74716d]&lt;[/color][color=#73706c]&lt;[/color][color=#73706c]&lt;[/color][color=#74706b]&lt;[/color][color=#676562]z[/color][color=#61605e]z[/color][color=#61605e]Ü[/color][color=#61605e]Ü[/color][color=#61605d]Ü[/color][color=#61605d]Ü[/color][color=#6b6a66]z[/color][color=#75716b]&lt;[/color][color=#74716c]&lt;[/color][color=#73716c]&lt;[/color][color=#73706c]&lt;[/color][color=#73706c]&lt;[/color][color=#73706c]&lt;[/color][color=#73706c]&lt;[/color][color=#73706b]&lt;[/color][color=#62615e]z[/color][color=#63625e]z[/color][color=#61615e]z[/color][color=#61605d]Ü[/color][color=#61615e]z[/color][color=#60605d]Ü[/color][color=#73706b]«[/color][color=#73706b]&lt;[/color][color=#74716c]&lt;[/color][color=#74706c]H[/color][color=#74716e]^[/color]
[color=#7f7f7f][/color][color=#7f7f7f] [/color]

[/font][/size]"""

_COLOR_RE = re.compile(r"\[color=#([0-9a-fA-F]{6})\](.*?)\[/color\]")
_STRIP_RE = re.compile(r"\[/?(?:size|font)[^\]]*\]|</?span[^>]*>")

Cell = Tuple[str, str]          # (hex colour, single character)
Line = List[Cell]


def parse(raw: str = RAW_BBCODE) -> List[Line]:
    """BBCode -> lines of (colour, char), trimmed to the art's bounding box."""
    lines: List[Line] = []
    for raw_line in raw.splitlines():
        cleaned = _STRIP_RE.sub("", raw_line)
        cells: Line = []
        for m in _COLOR_RE.finditer(cleaned):
            colour = m.group(1).lower()
            for ch in html.unescape(m.group(2)):
                cells.append((colour, ch))
        lines.append(cells)

    def blank(line: Line) -> bool:
        return all(ch == " " for _, ch in line)

    while lines and blank(lines[0]):
        lines.pop(0)
    while lines and blank(lines[-1]):
        lines.pop()

    # Remove the indentation common to every non-blank line, and trailing space.
    def indent(line: Line) -> int:
        n = 0
        for _, ch in line:
            if ch != " ":
                break
            n += 1
        return n

    common = min((indent(ln) for ln in lines if not blank(ln)), default=0)
    out: List[Line] = []
    for ln in lines:
        ln = ln[common:]
        while ln and ln[-1][1] == " ":
            ln.pop()
        out.append(ln)
    return out


ART = parse()
WIDTH = max((len(ln) for ln in ART), default=0)
HEIGHT = len(ART)


# --------------------------------------------------------------------------
# Colour handling
# --------------------------------------------------------------------------


def _rgb(hex_colour: str) -> Tuple[int, int, int]:
    return int(hex_colour[0:2], 16), int(hex_colour[2:4], 16), int(hex_colour[4:6], 16)


def adapt(hex_colour: str, background: str = "dark") -> Tuple[int, int, int]:
    """Make the art readable on the terminal's background.

    The logo was drawn for a light page: its outlines and the 'Doc' lettering
    are near-black navy, which disappears on a dark terminal. On dark
    backgrounds the neutral tones are inverted (dark ink becomes light ink,
    keeping its slight blue tint) while the saturated oranges and greens are
    left alone, apart from a floor on their brightness.
    """
    r, g, b = _rgb(hex_colour)
    if background != "dark":
        return r, g, b
    h, lightness, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    # Chroma, not HLS saturation, decides what counts as neutral: saturation
    # is unstable for near-black colours (the navy ink reads as 43% saturated),
    # which split the outline into a patchwork of grey and blue.
    chroma = (max(r, g, b) - min(r, g, b)) / 255
    if chroma < 0.16:                 # neutral: ink, outlines, lettering
        lightness = 1.0 - lightness
    elif lightness < 0.30:            # dark but coloured (juice-box shading)
        lightness = 0.30 + (lightness * 0.5)
    rr, gg, bb = colorsys.hls_to_rgb(h, lightness, s)
    return round(rr * 255), round(gg * 255), round(bb * 255)


def detect_background() -> str:
    """'dark' unless the environment says otherwise.

    DOCJUICE_BG=light|dark wins. Otherwise COLORFGBG (set by many terminals,
    e.g. '15;0' = white on black) is used, and dark is the fallback -- it is
    the default in Kali and most terminal themes.
    """
    forced = os.environ.get("DOCJUICE_BG", "").strip().lower()
    if forced in ("light", "dark"):
        return forced
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        try:
            bg = int(fgbg.split(";")[-1])
            return "light" if bg in (7, 15) else "dark"
        except ValueError:
            pass
    return "dark"


def _xterm256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 index, for terminals without truecolor."""
    def cube(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40

    cr, cg, cb = cube(r), cube(g), cube(b)
    levels = (0, 95, 135, 175, 215, 255)
    cube_rgb = (levels[cr], levels[cg], levels[cb])
    grey_idx = max(0, min(23, round((((r + g + b) / 3) - 8) / 10)))
    grey_val = 8 + grey_idx * 10

    def dist(c):
        return (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2

    if dist((grey_val,) * 3) < dist(cube_rgb):
        return 232 + grey_idx
    return 16 + 36 * cr + 6 * cg + cb


def supports_truecolor() -> bool:
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def fits_terminal(margin: int = 2) -> bool:
    return shutil.get_terminal_size((80, 24)).columns >= WIDTH + margin


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_ansi(background: Optional[str] = None, truecolor: Optional[bool] = None,
                indent: int = 2) -> str:
    """The logo as a string of ANSI escape codes (no dependencies)."""
    background = background or detect_background()
    truecolor = supports_truecolor() if truecolor is None else truecolor
    reset = "\033[0m"
    rows = []
    for line in ART:
        parts = [" " * indent]
        current = None
        for colour, ch in line:
            if ch == " ":
                parts.append(" ")
                continue
            if colour != current:
                r, g, b = adapt(colour, background)
                parts.append(
                    f"\033[38;2;{r};{g};{b}m" if truecolor
                    else f"\033[38;5;{_xterm256(r, g, b)}m"
                )
                current = colour
            parts.append(ch)
        parts.append(reset)
        rows.append("".join(parts))
    return "\n".join(rows)


def render_rich(console, background: Optional[str] = None, indent: int = 2) -> None:
    """Print the logo through a rich Console (it picks the colour depth)."""
    from rich.text import Text

    background = background or detect_background()
    for line in ART:
        text = Text(" " * indent)
        for colour, ch in line:
            if ch == " ":
                text.append(" ")
            else:
                r, g, b = adapt(colour, background)
                text.append(ch, style=f"rgb({r},{g},{b})")
        console.print(text, highlight=False, soft_wrap=True)


def plain() -> str:
    """The logo without colour -- mainly for tests and previews."""
    return "\n".join("".join(ch for _, ch in line) for line in ART)


if __name__ == "__main__":
    bg = "light" if "--light" in sys.argv else ("dark" if "--dark" in sys.argv else None)
    if "--plain" in sys.argv or not sys.stdout.isatty() and "--force" not in sys.argv:
        print(plain())
    elif not fits_terminal() and "--force" not in sys.argv:
        sys.exit(3)        # too narrow: let the caller fall back to the wordmark
    else:
        print(render_ansi(background=bg))
