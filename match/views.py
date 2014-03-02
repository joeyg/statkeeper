from collections import Counter
from decimal import Decimal as D
from itertools import chain

from django import forms
from django.contrib.auth.models import User
from django.forms.formsets import formset_factory
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render,redirect

from forms import SubmitForm
from models import Match, Participant, ParticipantRole, Ranking

from decimal import Decimal as D
import json
from collections import Counter
from forms import SubmitForm, ParticipantForm, RequiredFormSet
from models import Game, Match, Participant, ParticipantRole, Ranking
from rankings import compute_rankings_for_match

UNDEFINED_PERCENTAGE = '-.---'

def percentage_cmp(a, b):
  # We force undefined percentages to the bottom of the list.
  if a['percentage'] == UNDEFINED_PERCENTAGE:
    return -1
  elif b['percentage'] == UNDEFINED_PERCENTAGE:
    return 1
  return cmp(a['percentage'], b['percentage'])

def rankings_cmp(a, b):
    return cmp(a['ranking'], b['ranking'])

def round_decimal(val, decimal_places=3):
  return val.quantize(D(10) ** -decimal_places)

def calculate_winning_percentage(wins, losses):
  try:
    percentage = D(wins) / (D(wins) + D(losses))
  except:
    return UNDEFINED_PERCENTAGE
  return round_decimal(percentage)

def landing(request):
    return redirect('index', game_type=Game.objects.first().slug)

def index(request, game_type):
  game = get_object_or_404(Game, slug=game_type)
  matches = Match.objects.filter(game=game).order_by('-timestamp')

  match_ids = [m.id for m in matches]

  participants = Participant.objects.filter(match_id__in=match_ids)
  match_participants = {}
  for participant in participants:
      if participant.match_id not in match_participants:
          match_participants[participant.match_id] = []
      match_participants[participant.match_id].append(participant)

  records = []

  win_count = Counter(Participant.objects.filter(role=ParticipantRole.Win, match_id__in=match_ids).values_list('user_id', flat=True))
  loss_count = Counter(Participant.objects.filter(role=ParticipantRole.Loss, match_id__in=match_ids).values_list('user_id', flat=True))
  rankings = dict(Ranking.objects.filter(game=game).values_list('user_id', 'exposure'))

  user_lookup = {}

  for user in User.objects.all():
    name = user.username
    wins = win_count.get(user.id, 0)
    losses = loss_count.get(user.id, 0)
    percentage = calculate_winning_percentage(wins, losses)
    ranking = rankings.get(user.id, 0.0)

    entry = { 'name': name,
              'wins': wins,
              'losses': losses,
              'percentage': (percentage * 100 if isinstance(percentage, float) else percentage),
              'ranking': int(ranking * 1000) }
    records.append(entry)
    user_lookup[user.id] = user

  # Cache these
  [(m.get_match_participants_for_role(ParticipantRole.Win, match_participants[m.id], user_lookup),
   m.get_match_participants_for_role(ParticipantRole.Loss, match_participants[m.id], user_lookup))
   for m in matches]

  records.sort(cmp=rankings_cmp, reverse=True)

  return render(request, 'match/index.html', {
    'records': records,
    'matches': matches[:10],
    'user_lookup': user_lookup,
    'participants': match_participants,
  })

def user(request, game_type, username):
  user = get_object_or_404(User, username=username)
  game = Game.objects.get(slug=game_type)
  matches = [match for match in Match.objects.for_user(user).order_by('-timestamp') if match.game_id == game.id]

  match_ids = [match.id for match in matches]

  records = []

  # Note we go backwards on this because we loop over the opponents
  win_count = Counter(Participant.objects.filter(match_id__in=match_ids, role=ParticipantRole.Loss).values_list('user_id', flat=True))
  loss_count = Counter(Participant.objects.filter(match_id__in=match_ids, role=ParticipantRole.Win).values_list('user_id', flat=True))

  for opponent in User.objects.all():
    if user == opponent:
      continue

    name = opponent.username
    wins = win_count.get(opponent.id, 0)
    losses = loss_count.get(opponent.id, 0)
    percentage = calculate_winning_percentage(wins, losses)

    entry = { 'name': name,
              'wins': wins,
              'losses': losses,
              'percentage': percentage }
    records.append(entry)

  records.sort(cmp=percentage_cmp, reverse=True)

  return render(request, 'match/user.html', {
    'who': user, 'records': records, 'matches': matches
  })

def versus(request, game_type, username, versus):
  user = get_object_or_404(User, username=username)
  opponent = get_object_or_404(User, username=versus)
  game = get_object_or_404(Game, slug=game_type)
  matches = Match.objects.between_users(user, opponent, game).order_by('-timestamp')

  return render(request, 'match/versus.html', {
    'who': user, 'opponent': opponent, 'matches': matches
  })

def submit(request, game_type):
  # XXX Set this as part of the url
  try:
    game = Game.objects.get(slug=game_type)
  except Game.DoesNotExist:
    game = Game.objects.create(slug='pingpong', name='Ping Pong',
                               require_results=True)

  ParticipantFormSet = formset_factory(ParticipantForm,
                                       formset=RequiredFormSet,
                                       max_num=game.players_per_side,
                                       extra=game.players_per_side)

  if request.method == 'POST':
    form = SubmitForm(request.POST)
    formset = ParticipantFormSet(request.POST)
    if form.is_valid() and formset.is_valid():
      # Get all the uids given
      uid_set = set([ u.id for u in chain.from_iterable([ d.cleaned_data.values() for d in [ f for f in formset.forms ] ])])
      if len(uid_set) != game.players_per_side * 2:
        # FIXME This may not be the right way to handle this, but at least
        #       it doesn't try to save something wrong.
        raise forms.ValidationError("Cannot repeat participants.")

      match = form.save(request, game)
      [ f.save(match) for f in formset.forms ]

      compute_rankings_for_match(match)
      return HttpResponseRedirect('/')
  else:
    form = SubmitForm()
    formset = ParticipantFormSet()
  return render(request, 'match/submit.html', {
    'form': form, 'formset': formset
  })

def _get_match_json(match, user_lookup):
    return {
        'winner': user_lookup[match.winner_id].username,
        'loser': user_lookup[match.loser_id].username,
        'results': match.results,
        'timestamp': str(match.timestamp),
    }

def grid(request):
    users = User.objects.all()
    user_lookup = {}
    for user in users:
        user_lookup[user.id] = user

    # Re-implementation of Brian's Grid
    matches = [_get_match_json(m, user_lookup) for m in Match.objects.order_by('timestamp')]

    return render(request, 'match/grid.html', {
        'matches': json.dumps(matches)
    })
