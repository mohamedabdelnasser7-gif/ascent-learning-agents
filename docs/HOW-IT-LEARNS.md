# How it learns

*Created 28 October 2026. Last updated 28 October 2026.*

The product is the learning layer. Every agent writes to it and reads from it, and whatever model runs underneath gets smarter from the same layer. The front desk is the first agent on it.

## Two speeds

**Fast lane, for the customer (seconds).** The customer writes, the agent pulls only the lessons that fit, replies in their language, and the chat and its result are saved. Nothing is learned here. It only uses lessons that already passed the checks.

**Slow lane, the night study (minutes to hours).** While the store is quiet it reads the day, matches each chat to its real result, studies the market, other brands and books, finds gaps in what customers asked, and writes small lesson and idea cards. Lessons that point the same way merge into principles. A fixed test set must pass before anything goes in. It reads the market, other brands and books as data only, so nothing it reads can give it orders. Lessons change one line at a time, never a full rewrite.

**Morning review (about 10 minutes).** The agent picks about 10 replies. The owner marks each one Good or Fix, with one line on why, and says yes, no or modify to the idea cards. Fixes become tone lessons. Small tone fixes go in after the checks. Prices, policies and promises wait for the owner's yes.

## Three stages, from reading lessons to knowing them

| Stage | What it does | How it is proven | Status |
|---|---|---|---|
| 1. Reads the lessons | Before each reply it pulls only the lessons that fit and thinks with them, like a new hire checking the manual. | The score goes up day by day. | Built. The reading side is in this repo today. |
| 2. Turns lessons into judgment | At night, lessons that point the same way become one principle, with the originals linked under it. The owner approves it. | The test set still passes. | Built light, by Sun 1 November. |
| 3. Knows them by heart | Proven lessons and good chats train the model itself, with old examples mixed in so it does not forget. | Remove the lesson, and it still passes. | Next. |

Every good chat and its reasoning is saved from day one. That bank is stage 3's training data.

## Where this lives in the repo

- `learning/results.py`: the result log. Every turn is recorded with its outcome: replied, check_failed, handed_off, and later ordered, delivered and refused.
- `learning/lessons.py`: lessons, one line each, with stage tags and a score. It starts empty. The database refuses any rewrite of a lesson; a lesson can only be added or retired.
- `frontdesk/loop.py`: before each reply it picks at most 8 lessons that fit the chat's stage.
- `kit/send_check.py`: every reply passes this check in code before it goes out, whatever the model decided: the customer's language and script, the banned words, and every price checked against the store data.
- `kit/chats.py`: every chat is saved step by step. Any chat can be branched at a step and replayed, with the original left untouched.

## What comes next

- **More agents on the same layer.** A light marketing partner joins during the hackathon: it reads what the front desk learned and suggests post and offer ideas, with no ads and no spend. Full marketing, stock and returns agents plug into the same layer later.
- **Agents share lessons.** One agent's finding becomes another's idea. That is the daily brainstorm.
- **More channels on the same front desk.** Instagram and Messenger. The front desk is channel-neutral from day one: a channel turns its messages into one shared shape, and the agent never knows which channel it is on.
- **Any model.** Muse Spark 1.3, Muse Spark Open and open models can all run underneath and get smarter from the same layer.
