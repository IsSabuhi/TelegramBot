from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import QualityOption, VideoInfo


def quality_keyboard(info: VideoInfo, auto_label: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if auto_label:
        rows.append(
            [InlineKeyboardButton(f"Авто · {auto_label}", callback_data="dl:auto")]
        )

    row: list[InlineKeyboardButton] = []
    for option in info.qualities:
        row.append(InlineKeyboardButton(option.label, callback_data=option.callback))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if info.has_audio:
        rows.append([InlineKeyboardButton("Только аудио", callback_data="dl:audio")])

    rows.append([InlineKeyboardButton("Отмена", callback_data="dl:cancel")])
    return InlineKeyboardMarkup(rows)


def job_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data=f"x:{job_id}")]]
    )


def best_fitting(qualities: list[QualityOption]) -> QualityOption | None:
    fitting = [q for q in qualities if not q.over_limit]
    if fitting:
        return max(fitting, key=lambda q: q.height)
    return None
