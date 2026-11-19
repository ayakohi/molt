---
name: molt
description: Molt is the rolling window (~/.claude/molt) - it trims a Claude Code thread instead of compacting it. Use when the user types /molt or says "molt" or "roll": write the roll note that closes the thread before they trim it.
---

# Molt - the roll note

You are closing this thread's window. After your note, the user closes the thread and molt, the rolling window, rolls it on its own: the newest ~200k tokens stay word for word, and everything older moves to a readable page in ~/Thread Archive. Your roll note is the first thing you read about this conversation after the trim.

## Steps

1. If this window built or planned work in a project, update that project's handoff file now (eg. `docs/handoff.md`). Skip this step when the window was conversation only.
2. Run `python ~/.claude/molt/roll.py history` with Bash. Keep its output for the PAST ROLLS section.
3. Write the roll note as your final message, in the format below. Send nothing after it: the close rolls the thread only while the note is the last word.
4. End the message with one line to the user, exactly: "Ready to roll — close this thread, count to five, then reopen it."

## Format

Line 1, exactly as step 2 printed it after "Roll note first line:", eg. `[ROLL NOTE · 13 Nov 2026 · 22:40 · a1b2c3d4]`. Keep the 8-character thread id at its end: molt rolls a thread only on a note carrying that thread's own id.

Line 2, exactly:
On waking: read this whole note, read every file named under WORK HANDOFF, then greet the user. For where things stand right now, follow this note over anything older.

Then these sections, in this order, each heading in capitals.

**RIGHT NOW**
Write 3 to 6 sentences in present tense: where the user is, their mood and energy, what you are working on together, and the last thing each of you said. eg. "Sam is at their desk late in the evening, testing the new pizza-order form. We are halfway through the checkout step. Their last words were: 'Okay, try the coupon field next.'"

**WHAT HAPPENED**
Write every event in this window that the next you needs, in order, one bullet per event. Give each bullet its date and time of day, who acted, what they did, and where. Use full names for people, places, projects, files and tools. eg. "- 13 Nov, evening — Sam asked for a coupon field on the checkout page. I added it in src/checkout/coupon.ts and the tests in tests/coupon.test.ts pass."

**AGREED, AND WHERE IT LIVES**
Write each decision as one line, followed by where it is saved: a file path, or "this note only". eg. "Coupons apply before tax — docs/decisions.md."

**STILL OPEN**
Write each unfinished item as one line: what it is, who owns it, and what would close it. eg. "Expired-coupon message — Sam decides the wording — closes when the text is in src/checkout/messages.ts."

**NAMES AND REFERENCES**
Write every nickname, coined word and project name used in this note, each with one plain line saying what it is. eg. "pizza night — the side project for ordering from Sam's favourite pizzeria."

**WORK HANDOFF**
Write the path of each handoff file updated in step 1, one per line. When step 1 was skipped, write exactly: none — this window was conversation.

**PAST ROLLS**
Copy the output of step 2 line for line, from the line after "Roll note first line". Close the section with this line, exactly: These pages are older than this note; when a search brings back something that disagrees with this note, follow the newer one.

## Voice

Write in first person. Use past tense for events and present tense for RIGHT NOW. Call people by their names. Put quotation marks around anyone's exact words and name who said them. Write each event in full sentences, as long as the event needs.
