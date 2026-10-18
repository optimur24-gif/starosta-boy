"""Shared plain lesson fields for both Telegram bots."""

from collections import OrderedDict
import re


def short_teacher(value):
    if not value:
        return 'Преподаватель не указан'
    names = []
    for name in re.split(r'[,;\n]+', str(value)):
        words = name.split()
        if not words:
            continue
        if len(words) == 1 or any(word.casefold() in ('уточняется', 'указан', 'не') for word in words):
            names.append(' '.join(words))
            continue
        initials = []
        for word in words[1:]:
            if re.fullmatch(r'(?:[^\W\d_]\.)+', word):
                initials.append(word)
            else:
                initials.append('-'.join(part[0].upper()+'.' for part in word.split('-') if part))
        names.append(words[0] + ' ' + ''.join(initials))
    return ', '.join(names) or 'Преподаватель не указан'


def compact_details(details, *, show_subgroups=True):
    combined = OrderedDict()
    for detail in details:
        key = tuple(str(detail.get(field) or '') for field in ('groupId', 'teacher', 'room', 'building'))
        combined.setdefault(key, {'detail': detail, 'subgroups': set(), 'whole': False})
        if detail.get('subgroup'):
            combined[key]['subgroups'].add(str(detail['subgroup']))
        else:
            combined[key]['whole'] = True
    lines = []
    for row in combined.values():
        detail = row['detail']
        room = str(detail.get('room') or 'Аудитория не указана').strip()
        building = str(detail.get('building') or '').strip()
        if building and building not in room:
            room += ', ' + building
        prefix = ''
        if show_subgroups and row['subgroups'] and not row['whole']:
            prefix = 'подгр. ' + ', '.join(sorted(row['subgroups'])) + ': '
        lines.append(prefix + room + ' · ' + short_teacher(detail.get('teacher')))
    return tuple(lines) or ('Аудитория не указана · Преподаватель не указан',)


def lesson_kind(value):
    """Keep the site's lesson type visible, using compact Russian labels."""
    text = str(value or '').strip()
    normalized = text.casefold().replace('ё','е')
    if normalized.startswith('лек'):
        return '🎓 Лекция'
    if normalized.startswith('практ'):
        return '🧪 Практика'
    if normalized.startswith('лаб'):
        return '🔬 Лабораторная'
    if normalized.startswith('семинар'):
        return '💬 Семинар'
    if normalized.startswith('экзам'):
        return '📝 Экзамен'
    if normalized.startswith(('зачет','дифференцирован')):
        return '✅ '+text
    return '📚 '+(text or 'Занятие')


DAY_NAMES = ('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')


def day_label(day):
    return f'{day:%d.%m.%Y} · {DAY_NAMES[day.weekday()]}'
