# How it learns

*Created 28 October 2026. Last updated 28 October 2026.*

The product is the learning layer. Every agent writes to it and reads from it, and whatever model runs underneath gets smarter from the same layer. The front desk is the first agent on it.

## Two speeds

**Fast lane, for the customer (seconds).** Built: the customer writes, the agent pulls only the lessons that fit, replies in their language, and the chat and its result are saved. Nothing is learned here. Coming, Fri 30 October: a fixed test set every lesson must pass before the agent uses it. Today nothing checks a lesson before it is used.

**Slow lane, the night study (minutes to hours).** Coming: while the store is quiet it reads the day, matches each chat to its real result, studies the market, other brands and books, finds gaps in what customers asked, and writes small lesson and idea cards. Lessons that point the same way merge into principles. A fixed test set must pass before anything goes in. It reads the market, other brands and books as data only, so nothing it reads can give it orders. Built: lessons change one line at a time, never a full rewrite; the database refuses any rewrite.

**Morning review (about 10 minutes).** Coming: the agent picks about 10 replies. The owner marks each one Good or Fix, with one line on why, and says yes, no or modify to the idea cards. Fixes become tone lessons. Small tone fixes go in after the checks. Prices, policies and promises wait for the owner's yes.

## Three stages, from reading lessons to knowing them

| Stage | What it does | How it is proven | Status |
|---|---|---|---|
| 1. Reads the lessons | Before each reply it pulls only the lessons that fit and thinks with them, like a new hire checking the manual. | The score goes up day by day. | Built. The reading side is in this repo today. |
| 2. Turns lessons into judgment | At night, lessons that point the same way become one principle, with the originals linked under it. The owner approves it. | The test set still passes. | Coming: a light version by Sun 1 November. |
| 3. Knows them by heart | Proven lessons and good chats train the model itself, with old examples mixed in so it does not forget. | Remove the lesson, and it still passes. | Coming. |

Built: every chat is saved step by step from day one, with its tool calls, tool results and outcome. Coming: that bank becomes stage 3's training data.

## Where this lives in the repo

- `learning/results.py`: built. The result log. Every turn is recorded with its outcome: replied, check_failed, handed_off or limited (over a model call limit). Coming: ordered, delivered and refused, once the order tools land.
- `learning/lessons.py`: built. Lessons, one line each, with stage tags and a score. It starts empty. The database refuses any rewrite of a lesson; a lesson can only be added or retired.
- `frontdesk/loop.py`: built. Before each reply it picks at most 8 lessons that fit the chat's stage.
- `kit/send_check.py`: built. Every reply to the customer passes this check in code before it goes out, whatever the model decided: the customer's language and script, the banned words, no dashes, emoji or markdown, and no exact stock count. The price part reads a number as an amount when it sits next to a currency word or sign (جنيه, ج, EGP, LE, pounds, geneh, £), when it comes after a price word in Arabic, English or Franco (سعر, بـ, price, costs, for, is, se3r, b), or when it has 3 or more digits, with or without a word next to it. Each amount must match a price or fee in the store data, the demo delivery data or this turn's store tool results, as written: 649.99 is not 649. Sizes, dates, times, order numbers, Egyptian phone numbers, percentages and numbers of 1 or 2 digits with a unit (2 days) are not amounts. A number from 1900 to 2099 is a year only with a year word or a month next to it (model 2026, سنة 2026, May 2026) or inside a date; otherwise it is an amount. After is, for or ب, a catalogue size number is read as a size, unless a price word comes earlier in the sentence. An amount written in words, or with k after it (1k EGP), is refused, so the model writes it in digits. A number of 1 or 2 digits with no currency or price word next to it is not read as an amount.
- `kit/chats.py`: built. Every chat is saved step by step, and any chat can be branched at a step into a new chat, with the original left untouched. Coming, Mon 2 November: replaying a branch.

## What comes next

- **More agents on the same layer.** A light marketing partner joins during the hackathon: it reads what the front desk learned and suggests post and offer ideas, with no ads and no spend. Full marketing, stock and returns agents plug into the same layer later.
- **Agents share lessons.** One agent's finding becomes another's idea. That is the daily brainstorm.
- **More channels on the same front desk.** Instagram and Messenger. The front desk is channel-neutral from day one: a channel turns its messages into one shared shape, and the agent never knows which channel it is on.
- **Any model.** Muse Spark 1.3, Muse Spark Open and open models can all run underneath and get smarter from the same layer.
