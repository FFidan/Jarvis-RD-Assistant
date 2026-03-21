import { useState } from 'react';
import { GraduationCap, Plus, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { StatsHeader } from '@/components/cards/StatsHeader';
import { ReviewMode } from '@/components/cards/ReviewMode';
import { DeckBrowser } from '@/components/cards/DeckBrowser';
import { CardList } from '@/components/cards/CardList';
import { CreateCardForm, GenerateCardsDialog } from '@/components/cards/CreateCardForm';

export function LearningCardsPage() {
  const [selectedDeckId, setSelectedDeckId] = useState<number | null>(null);
  const [showCreateCard, setShowCreateCard] = useState(false);
  const [showGenerate, setShowGenerate] = useState(false);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <GraduationCap className="h-8 w-8" /> Learning Cards
        </h1>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setShowGenerate(true)}>
            <Sparkles className="mr-1 h-4 w-4" /> Generate
          </Button>
          <Button onClick={() => setShowCreateCard(true)}>
            <Plus className="mr-1 h-4 w-4" /> New Card
          </Button>
        </div>
      </div>
      <p className="text-muted-foreground text-sm">Spaced repetition flashcards generated from your papers</p>

      <StatsHeader />

      <Tabs defaultValue="review">
        <TabsList>
          <TabsTrigger value="review">Review</TabsTrigger>
          <TabsTrigger value="browse">Browse</TabsTrigger>
        </TabsList>

        <TabsContent value="review" className="mt-4">
          <ReviewMode />
        </TabsContent>

        <TabsContent value="browse" className="mt-4 space-y-6">
          <DeckBrowser
            selectedDeckId={selectedDeckId}
            onSelectDeck={setSelectedDeckId}
          />
          {selectedDeckId && (
            <div className="space-y-3">
              <h3 className="text-lg font-medium">Cards</h3>
              <CardList deckId={selectedDeckId} />
            </div>
          )}
        </TabsContent>
      </Tabs>

      <CreateCardForm
        open={showCreateCard}
        onOpenChange={setShowCreateCard}
        defaultDeckId={selectedDeckId}
      />
      <GenerateCardsDialog
        open={showGenerate}
        onOpenChange={setShowGenerate}
        defaultDeckId={selectedDeckId}
      />
    </div>
  );
}
