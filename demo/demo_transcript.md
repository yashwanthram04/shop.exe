# Demo transcript


================================================================
[BUYING — a quick hit]  sample_id=public_0005  target=B074G1JP8Z
================================================================

Turn 1
  Customer : I'm looking for Outdoor & Work Snow & Cold Weather. A key requirement is: leather.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : HIT, rank 1

RESULT: HIT at turn 1, rank 1

==============================================================================
[BROWSING — starts vague, converges]  sample_id=public_0087  target=B0BT158RRR
==============================================================================

Turn 1
  Customer : I'm looking for Shirts Casual Button-Down Shirts, but I'm still exploring.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : miss

Turn 2
  Customer : For that, what matters is: cotton; 100% Cotton.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : miss

Turn 3
  Customer : For that, what matters is: Imported; Button closure.
  Agent    : 'Do you have a brand preference?'  (ask_attribute='brand')
  Top-10   : miss

Turn 4
  Customer : I don't have an additional preference for brand.
  Agent    : 'Do you have a color preference?'  (ask_attribute='color')
  Top-10   : HIT, rank 1

RESULT: HIT at turn 4, rank 1

=================================================================================
[BOUNDARY — customer has no preference]  sample_id=public_0180  target=B01HSMYV8E
=================================================================================

Turn 1
  Customer : I'm looking for Shoes Fashion Sneakers, but I'm still exploring.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : miss

Turn 2
  Customer : I don't have a preference for other; please use your judgment.
  Agent    : "Here are some options based on what you've told me so far."  (ask_attribute=None)
  Top-10   : miss

Turn 3
  Customer : Those options are not quite right yet. Ask me about one specific attribute.
  Agent    : 'Do you have a brand preference?'  (ask_attribute='brand')
  Top-10   : miss

Turn 4
  Customer : I don't have an additional preference for brand.
  Agent    : 'Do you have a color preference?'  (ask_attribute='color')
  Top-10   : miss

Turn 5
  Customer : I don't have an additional preference for color.
  Agent    : 'Do you have a style preference?'  (ask_attribute='style')
  Top-10   : miss

Turn 6
  Customer : I don't have an additional preference for style.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : miss

Turn 7
  Customer : For that, what matters is: 100% Mesh; Imported.
  Agent    : "Here are some options based on what you've told me so far."  (ask_attribute=None)
  Top-10   : HIT, rank 8

RESULT: HIT at turn 7, rank 8

=========================================================================================
[INTENT OVERRIDE — customer changes their mind]  sample_id=public_0068  target=B08SH8GF6K
=========================================================================================

Turn 1
  Customer : I'm looking for Athletic Walking. Lightweight and responsive Ultra Go midsole cushioning
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : HIT, rank 2

Turn 2
  Customer : For that, what matters is: Imported; Rubber sole.
  Agent    : 'Is there anything else that matters to you?'  (ask_attribute='other')
  Top-10   : miss

Turn 3
  Customer : Actually, ignore my earlier preference. What I need is: Imported.
  Agent    : 'Do you have a brand preference?'  (ask_attribute='brand')
  Top-10   : miss

Turn 4
  Customer : I don't have an additional preference for brand.
  Agent    : 'Do you have a color preference?'  (ask_attribute='color')
  Top-10   : miss

Turn 5
  Customer : I don't have an additional preference for color.
  Agent    : 'Do you have a style preference?'  (ask_attribute='style')
  Top-10   : HIT, rank 9

RESULT: HIT at turn 5, rank 9

============================================================
SUMMARY
============================================================
  buying           public_0005    turn 1, rank 1
  browsing         public_0087    turn 4, rank 1
  boundary         public_0180    turn 7, rank 8
  intent_override  public_0068    turn 5, rank 9
