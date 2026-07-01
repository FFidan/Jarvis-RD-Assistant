import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { SetupStep } from '@/components/setup/SetupStep';
import { createTopic, getSetupStatus } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { errorMessage } from '@/lib/errors';
import type { Topic } from '@/types';
import type { StepNavProps } from './shared';

interface FirstTopicStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
}

export function FirstTopicStep({ stepNumber, totalSteps, onBack, onNext }: FirstTopicStepProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [added, setAdded] = useState(false);

  const { data: setupStatus } = useQuery({
    queryKey: QUERY_KEYS.setup.status(),
    queryFn: getSetupStatus,
    staleTime: 30_000,
  });
  const hasTopics = (setupStatus?.topics_count ?? 0) > 0;

  const createMut = useMutation({
    mutationFn: (data: Partial<Topic>) => createTopic(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
      setAdded(true);
      setName('');
      setDescription('');
    },
    onError: (err: Error) => {
      console.error('Failed to create topic', err);
    },
  });

  const handleAdd = () => {
    if (!name.trim()) return;
    createMut.mutate({
      name: name.trim(),
      query_terms: [name.trim()],
      description: description.trim() || null,
    } as Partial<Topic>);
  };

  const topicConfigured = added || hasTopics;

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Your first research topic"
      description="Topics drive paper discovery. You can add more later in Settings."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {topicConfigured ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <Label htmlFor="setup-topic-name">Topic name</Label>
          <Input id="setup-topic-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Neural ODEs" />
        </div>
        <div>
          <Label htmlFor="setup-topic-description">Description (optional)</Label>
          <Textarea
            id="setup-topic-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this topic is about (helps Pulse find relevant papers)"
            rows={3}
            maxLength={1000}
          />
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={handleAdd} disabled={!name.trim() || createMut.isPending}>
            Add topic
          </Button>
          {topicConfigured && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {added ? 'Topic added' : `${setupStatus?.topics_count} topic(s) configured`}
            </span>
          )}
        </div>
        {createMut.isError && (
          <p className="text-sm text-destructive">{errorMessage(createMut.error, 'Could not add topic — try again.')}</p>
        )}
      </div>
    </SetupStep>
  );
}
