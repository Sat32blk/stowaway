<ul class="livestats">
@if(isset($items))
    @foreach($items as $it)
    <li>
        <span class="title">{{ $it['label'] }}</span>
        <strong style="color: {{ $it['color'] }}">{{ $it['value'] }}</strong>
    </li>
    @endforeach
@else
    <li>
        <span class="title">Status</span>
        <strong style="color: {{ $color ?? '#909296' }}">{{ $state ?? 'Unknown' }}</strong>
    </li>
@endif
</ul>
