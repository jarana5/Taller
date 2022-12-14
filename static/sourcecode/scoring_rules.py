from abc import ABC, abstractmethod
from collections import namedtuple
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple

import constants as c, tag_filter

import numpy as np
import pandas as pd


RuleAndVersion = namedtuple("RuleAndVersion", ["ruleName", "ruleVersion"])
"""namedtuple identifying ScoringRule with a name and tracking revisions with a version."""


class RuleID(Enum):
  """Each RuleID must have a unique ruleName and can be assigned to at most one ScoringRule."""

  INITIAL_NMR = RuleAndVersion("InitialNMR", "1.0")
  GENERAL_CRH = RuleAndVersion("GeneralCRH", "1.0")
  GENERAL_CRNH = RuleAndVersion("GeneralCRNH", "1.0")
  TAG_OUTLIER = RuleAndVersion("TagFilter", "1.0")


class ScoringRule(ABC):
  """Scoring logic describing how to assign a ratingStatus given raw scoring signals and note attributes.

  Each ScoringRule must have a name, version and status (e.g. CRH, NMR, etc.) which the rule will
  assign.  Each ScoringRule must implement a score_notes function, which accepts as input the
  raw attributes of notes and currently assigned lables and returns (1) a set of noteIDs which the rule
  will act on, and (2) a DF containing any new columns which should be added to the output for those
  noteIDs.  Note that score_notes does not need to return the ratingStatus or the rule name/version,
  which are attributes of the object itself.
  """

  def __init__(self, ruleID: RuleID, status: str, dependencies: Set[RuleID]):
    """Create a ScoringRule.

    Args:
      rule: enum corresponding to a namedtuple defining a rule name and version string for the ScoringRule.
      status: valid ratingStatus to assign to notes where the ScoringRule is active.
      dependencies: Rules which must run before this rule can run.
    """
    self._ruleID = ruleID
    self._status = status
    self._dependencies = dependencies

  def get_rule_id(self) -> RuleID:
    """Returns the RuleID uniquely identifying this ScoringRule."""
    return self._ruleID

  def get_name(self) -> str:
    """Returns a string combining the name and version to uniquely name the logic of the ScoringRule."""
    return f"{self._ruleID.value.ruleName} (v{self._ruleID.value.ruleVersion})"

  def get_status(self) -> str:
    """Returns a string defining the ratingStatus for notes where this ScoringRule is active."""
    return self._status

  def check_dependencies(self, priorRules: Set[RuleID]) -> None:
    """Raise an AssertionError if rule dependencies have not been satisfied."""
    assert not (self._dependencies - priorRules)

  @abstractmethod
  def score_notes(
    self, noteStats: pd.DataFrame, currentLabels: pd.DataFrame
  ) -> (Tuple[pd.Series, Optional[pd.DataFrame]]):
    """Identify which notes the ScoringRule should be active for, and any new columns to add for those notes.

    Args:
      noteStats: Raw note attributes, scoring signals and attirbutes for notes.
      currentLabels: the ratingStatus assigned to each note from prior ScoringRules.

    Returns:
      Tuple[0]: note IDs where the ScoringRule is active.
      Tuple[1]: DF containing noteIDs and any new columns to add to output
    """


class DefaultRule(ScoringRule):
  def score_notes(
    self, noteStats: pd.DataFrame, currentLabels: pd.DataFrame
  ) -> (Tuple[pd.Series, Optional[pd.DataFrame]]):
    """Returns all noteIDs to initialize all note ratings to a default status (e.g. NMR)."""
    return (noteStats[c.noteIdKey], None)


class RuleFromFunction(ScoringRule):
  def __init__(
    self,
    ruleID: RuleID,
    status: str,
    dependencies: Set[RuleID],
    function: Callable[[pd.DataFrame], pd.Series],
  ):
    """Creates a ScoringRule which wraps a boolean function.

    Args:
      function: accepts noteStats as input and returns a boolean pd.Series corresponding to
        rows matched by the function.  For example, a valid function would be:
        "lambda noteStats: noteStats[c.noteInterceptKey] > 0.4"
    """
    super().__init__(ruleID, status, dependencies)
    self._function = function

  def score_notes(
    self, noteStats: pd.DataFrame, currentLabels: pd.DataFrame
  ) -> (Tuple[pd.Series, Optional[pd.DataFrame]]):
    """Returns noteIDs for notes matched by the boolean function."""
    return (noteStats.loc[self._function(noteStats)][c.noteIdKey], None)


class FilterTagOutliers(ScoringRule):
  def __init__(
    self,
    ruleID: RuleID,
    status: str,
    dependencies: Set[RuleID],
    tagRatioPercentile: int,
    minAdjustedTotal: float,
    crhSuperThreshold: float,
  ):
    """Filter CRH notes for outliers with high levels of any particular tag.

    Args:
      tagRatioPercentile: For a filter to trigger, the adjusted ratio value for a
        tag must exceed Nth percentile for notes currently rated as CRH.
      minAdjustedTotal: For a filter to trigger, the adjusted total of a tag must
        exceed the minAdjustedTotal.
      crhSuperThrehsold: If the note intercept exceeds the crhSuperThreshold, then the
        tag filter is disabled.
    """
    super().__init__(ruleID, status, dependencies)
    self._tagRatioPercentile = tagRatioPercentile
    self._minAdjustedTotal = minAdjustedTotal
    self._crhSuperThreshold = crhSuperThreshold

  def score_notes(
    self, noteStats: pd.DataFrame, currentLabels: pd.DataFrame
  ) -> (Tuple[pd.Series, pd.DataFrame]):
    """Identifies notes on track for CRH with high levels of any tag and assigns NMR status."""
    # Prune noteStats to only include CRH notes.
    crhNotes = currentLabels[currentLabels[c.ratingStatusKey] == c.currentlyRatedHelpful][
      [c.noteIdKey]
    ]
    crhStats = noteStats.merge(crhNotes, on=c.noteIdKey, how="inner")
    print(f"CRH notes prior to tag filtering: {len(crhStats)}")
    print(
      f"CRH notes above crhSuperThreshold: {sum(crhStats[c.noteInterceptKey] > self._crhSuperThreshold)}"
    )
    # Identify impacted notes.
    thresholds = tag_filter.get_tag_thresholds(crhStats, self._tagRatioPercentile)
    impactedNotes = pd.DataFrame.from_dict({c.noteIdKey: [], c.activeFilterTagsKey: []}).astype(
      {c.noteIdKey: np.int64}
    )
    print("Checking note tags:")
    for tag in c.notHelpfulTagsTSVOrder:
      adjustedColumn = f"{tag}{c.adjustedSuffix}"
      adjustedRatioColumn = f"{adjustedColumn}{c.ratioSuffix}"
      print(tag)
      print(f"  ratio threshold: {thresholds[adjustedRatioColumn]}")
      if tag == c.notHelpfulHardToUnderstandKey or tag == c.notHelpfulNoteNotNeededKey:
        print(f"outliner filtering disabled for tag: {tag}")
        continue
      tagFilteredNotes = crhStats[
        # Adjusted total must pass minimum threhsold set across all tags.
        (crhStats[adjustedColumn] > self._minAdjustedTotal)
        # Adjusted ratio must exceed percentile based total for this specific tag.
        & (crhStats[adjustedRatioColumn] > thresholds[adjustedRatioColumn])
        # Note intercept must be lower than crhSuperThreshold which overrides tag filter.
        & (crhStats[c.noteInterceptKey] < self._crhSuperThreshold)
      ][c.noteIdKey]
      impactedNotes = pd.concat(
        [impactedNotes, pd.DataFrame({c.noteIdKey: tagFilteredNotes, c.activeFilterTagsKey: tag})]
      )
    # log and consolidate imapcted notes
    print(f"Total {{note, tag}} pairs where tag filter logic triggered: {len(impactedNotes)}")
    impactedNotes = impactedNotes.groupby(c.noteIdKey).aggregate(list).reset_index()
    impactedNotes[c.activeFilterTagsKey] = [
      ",".join(tags) for tags in impactedNotes[c.activeFilterTagsKey]
    ]
    print(f"Total unique notes impacted by tag filtering: {len(impactedNotes)}")
    return (impactedNotes[c.noteIdKey].drop_duplicates(), impactedNotes)


def apply_scoring_rules(noteStats: pd.DataFrame, rules: List[ScoringRule]) -> pd.DataFrame:
  """Apply a list of ScoringRules to note inputs and return noteStats augmented with scoring results.

  This function applies a list of ScoringRules in order.  Once each rule has run
  a final ratingStatus is set for each note. An additional column is added to capture
  which rules acted on the note and any additional columns generated by the ScoringRules
  are merged with the scored notes to generate the final return value.

  Args:
    noteStats: attributes, aggregates and raw scoring signals for each note.
    rules: ScoringRules which will be applied in the order given.

  Returns:
    noteStats with additional columns representing scoring results.
  """
  # Initialize empty dataframes to store labels for each note and which rules impacted
  # scoring for each note.
  noteLabels = pd.DataFrame.from_dict({c.noteIdKey: [], c.ratingStatusKey: []}).astype(
    {c.noteIdKey: np.int64}
  )
  noteRules = pd.DataFrame.from_dict({c.noteIdKey: [], c.activeRulesKey: []}).astype(
    {c.noteIdKey: np.int64}
  )
  noteColumns = pd.DataFrame.from_dict({c.noteIdKey: []}).astype({c.noteIdKey: np.int64})
  # Establish state to enforce rule dependencies.
  ruleIDs: Set[RuleID] = set()
  # Successively apply each rule
  for rule in rules:
    print(f"Applying scoring rule: {rule.get_name()}")
    rule.check_dependencies(ruleIDs)
    assert rule.get_rule_id() not in ruleIDs, f"repeate ruleID: {rule.get_name()}"
    ruleIDs.add(rule.get_rule_id())
    activeNotes, additionalColumns = rule.score_notes(noteStats, noteLabels)
    if additionalColumns is not None:
      assert set(activeNotes) == set(additionalColumns[c.noteIdKey])
    # Update noteLabels, which will always hold at most one label per note.
    noteLabels = (
      pd.concat(
        [
          noteLabels,
          pd.DataFrame.from_dict({c.noteIdKey: activeNotes, c.ratingStatusKey: rule.get_status()}),
        ]
      )
      .groupby(c.noteIdKey)
      .tail(1)
    )
    # Update note rules to have one row per rule which was active for a note
    noteRules = pd.concat(
      [
        noteRules,
        pd.DataFrame.from_dict({c.noteIdKey: activeNotes, c.activeRulesKey: rule.get_name()}),
      ]
    )
    # Merge any additional columns into current set of new columns
    if additionalColumns is not None:
      assert {c.noteIdKey} == (set(noteColumns.columns) & set(additionalColumns.columns))
      noteColumns = noteColumns.merge(additionalColumns, on=c.noteIdKey, how="outer")
  # Having applied all scoring rules, condense noteRules to have one row per note representing
  # all of the ScoringRuless which were active for the note.
  noteRules = noteRules.groupby(c.noteIdKey).aggregate(list).reset_index()
  noteRules[c.activeRulesKey] = [
    ",".join(activeRules) for activeRules in noteRules[c.activeRulesKey]
  ]
  # Validate that there are labels and assigned rules for each note
  assert set(noteStats[c.noteIdKey]) == set(noteLabels[c.noteIdKey])
  assert set(noteStats[c.noteIdKey]) == set(noteRules[c.noteIdKey])
  assert len(set(noteColumns[c.noteIdKey]) - set(noteStats[c.noteIdKey])) == 0
  # Merge note labels, active rules and new columns into noteStats to form scoredNotes
  scoredNotes = noteStats.merge(noteLabels, on=c.noteIdKey, how="inner")
  scoredNotes = scoredNotes.merge(noteRules, on=c.noteIdKey, how="inner")
  scoredNotes = scoredNotes.merge(noteColumns, on=c.noteIdKey, how="left")
  assert len(scoredNotes) == len(noteStats)
  # Set boolean columns indicating scoring outcomes
  scoredNotes[c.currentlyRatedHelpfulBoolKey] = (
    scoredNotes[c.ratingStatusKey] == c.currentlyRatedHelpful
  )
  scoredNotes[c.currentlyRatedNotHelpfulBoolKey] = (
    scoredNotes[c.ratingStatusKey] == c.currentlyRatedNotHelpful
  )
  scoredNotes[c.awaitingMoreRatingsBoolKey] = scoredNotes[c.ratingStatusKey] == c.needsMoreRatings
  # Return completed DF including original noteStats signals merged wtih scoring results
  return scoredNotes
