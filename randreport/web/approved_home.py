"""Bind the owner-approved homepage template to public projections only."""
from pathlib import Path
from string import Template

from . import render
from .home_finance_approved import finance

_TEMPLATE = Template(Path(__file__).with_name('home_approved.html').read_text(encoding='utf8'))
_MONTH = ('декабрь','январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь')


#: The subtitle and the honest empty line used while a release predates the
#: counters (or when the caller passes no announcement section at all).
DEFAULT_RANKING_NOTE = 'ТОП-3 АНОНСА · ЗА ПОСЛЕДНИЕ 30 ДНЕЙ'
DEFAULT_RANKING_EMPTY = 'Нет данных о пересылках за 30 дней.'


def announcements_view(announce):
    """The render inputs of the «Что пересылали» block from its public DTO.

    Returns ``(cards, note, empty_text)``.  The subtitle always names the real
    window dates, so the sliding ranking can never be mistaken for the selected
    report month (the task's §7).  ``fetched_at is None`` means Telegram has
    never been measured — the block says so instead of printing zeros; a
    measured window without qualifying announcements says that instead.
    """

    if announce is None:
        return (), DEFAULT_RANKING_NOTE, DEFAULT_RANKING_EMPTY
    cards = tuple(
        {'title': item.title, 'url': item.url, 'forwards': int(item.forwards)}
        for item in announce.items
    )
    note = f"ТОП-3 АНОНСА · {announce.window_from:%d.%m}—{announce.window_to:%d.%m.%Y}".upper()
    if announce.fetched_at is None:
        empty = ('Статистика пересылок канала ещё ни разу не получалась — '
                 'рейтинг появится после первого измерения.')
    elif not announce.items:
        empty = ('За последние 30 дней анонсов с измеренными пересылками нет — '
                 'либо их не публиковали, либо счётчики пока неизвестны.')
    else:
        empty = DEFAULT_RANKING_EMPTY
    return cards, note, empty


def _ranking(items, empty_text=DEFAULT_RANKING_EMPTY):
    eligible = []
    for item in items:
        count = item.get('forwards')
        url = render.safe_url(item.get('url'))
        if url and type(count) is int and count >= 0:
            eligible.append((count, url, item.get('title') or 'Без названия'))
    if not eligible:
        return f'<p class="financial-note">{render.e(empty_text)}</p>'
    cards = []
    for rank, (count, url, title) in enumerate(sorted(eligible, key=lambda row: -row[0])[:3], 1):
        cards.append(f'<a class="forward" href="{render.e(url)}" rel="noopener"><span class="rank">№ {rank:02d}</span>'
                     f'<span class="post-title">{render.e(title)}</span><span class="post-bottom"><span class="forwards">↗ {count} '
                     f'{render.plural(count,"пересылка","пересылки","пересылок")}</span><span class="arrows" aria-hidden="true">↗</span></span></a>')
    return '<div class="trend-grid">'+''.join(cards)+'</div>'


def page(*, period_label, money, events, community, historical, events_previous=None,
         subscribers_extra=(), announcements=(), ranking_note=DEFAULT_RANKING_NOTE,
         ranking_empty=DEFAULT_RANKING_EMPTY, photo_url=None, logo_url=None, status=''):
    e = render.e
    count = events.metrics.get('total_events') if events else None
    if count is None and historical.get('events') is not None:
        count = historical['events'].value
    subs = community.metrics.get('latest_subscribers') if community else None
    points = community.channel_points if community else ()
    delta, base_on, span = render.subscriber_delta([*points, *subscribers_extra]) if points and subs is not None else (None,None,None)
    # An exact 30-day base answers with the real change; without one the sheet says
    # so honestly (never a zero, never the nearest older snapshot dressed up as 30
    # days). The dated formulation and the interval live in the subline's hint.
    if delta is None or span != 30:
        delta_text = 'Нет сравнения за 30 дней'
        # Short, plaque-sized hint: the tooltip stays a small overlay, so the long
        # reasoning about dated snapshots lives in the methodology section instead.
        delta_hint = 'Базы ровно за 30 суток в публичных выпусках нет — старым замером прирост не подменяем.'
    else:
        delta_text = f'{"↗ +" if delta >= 0 else "↘ −"}{render._thousands(abs(delta))} за {span} {render.plural(span,"день","дня","дней")}'
        delta_hint = f'Изменение подписчиков за 30 суток по датированным снимкам; база — снимок на {base_on}.'
    month = render.month_in(period_label) if len(period_label)==7 else None
    prev_label = _MONTH[int(period_label[-2:])-1] if month else 'Прошлый месяц'
    previous = f'{prev_label}: {render._thousands(events_previous)}' if month else 'За выбранный период'
    previous_hint = 'Число событий за прошлый календарный месяц'
    photo = (f'<img class="visual-cut photo" src="{e(photo_url)}" alt="Фотография подвала РанДома">' if photo_url in {f'/static/{name}' for name in render.PHOTO_ASSETS}
             else '<span class="missing-photo" role="img" aria-label="Фотография пока недоступна"><span class="sr-only">Фото появится, когда будет разрешённый к публикации снимок.</span></span>')
    # The owner's logo replaces the asterisk next to the title; without the asset
    # the decorative mark stays, so a plain static contour never shows a broken image.
    brand_mark = (f'<img class="brand-mark" src="{e(logo_url)}" alt="РанДом Культуры">' if logo_url
                  else '<span class="star" aria-hidden="true">✳</span>')
    return _TEMPLATE.substitute(period=e(period_label), photo='<figure class="visual'+(' has-photo' if photo.startswith('<img') else '')+'" aria-label="Подвал РанДома"><div class="understrip" aria-hidden="true"></div>'+photo+'<div class="understrip bottom" aria-hidden="true"></div></figure>',
        events=e(render._thousands(count)), subscribers=e(render._thousands(subs)),
        events_wide=' wide' if count is None else '', subs_wide=' wide' if subs is None else '',
        events_label=e((render.plural(count,'событие','события','событий')+(f' в {month}' if month else ' за период')).upper()),
        subs_label=e(render.plural(subs,'подписчик','подписчика','подписчиков').upper()), previous=e(previous), delta=e(delta_text),
        previous_hint=e(previous_hint), delta_hint=e(delta_hint), brand_mark=brand_mark,
        finance=finance(money,period_label,historical), ranking_note=e(ranking_note),
        ranking=_ranking(announcements, ranking_empty),
        navigation=render.nav_cuts('/',period_label).replace('paper-sand','paper-yellow').replace('Разделы листка','Разделы сайта'),status=status)
