---
name: Feature request / improvement
about: A better way to do something, or a missing capability
title: ''
labels: enhancement
assignees: ''
---

## The problem

What goes wrong today. Concrete beats abstract — a real request that produced a
bad work order is worth more than a description of the category.

## What you'd change

## Would it be code or a model?

Worth thinking about explicitly, because getting this wrong has cost time here.
Derivable facts belong in code — the verification gate started as "have a second
model police it" and ended up deterministic, which turned out strictly better
(it correctly omits `git diff` in projects that are not git repos, where a model
suggested it anyway).

Judgement — is this plan actually sound? — is where a model earns its cost.

## Anything already tried
